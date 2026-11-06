"""Structured effect checks. Source interpretations remain explicit scientific judgments.

Mirrors site/lib/targets.js. Never infer units or arm roles from an effect's magnitude.
"""
import math

RATIOS = {"risk_ratio", "odds_ratio", "hazard_ratio"}
UNITS = {
    "mean_difference": {"original_units"},
    "standardized_mean_difference": {"standardized_units"},
    "risk_difference": {"proportion", "percentage_points"},
    **{metric: {"ratio"} for metric in RATIOS},
}


def metric_of(metric):
    return "standardized_mean_difference" if metric == "cohen_d" else metric


def effect_details(values, method):
    first = values[0]
    if method == "reported":
        metric = first["statistic"]
        unit = first.get("unit", "unknown")
        if unit == "unknown":
            if metric in RATIOS:
                unit = "ratio"
            elif metric == "standardized_mean_difference":
                unit = "standardized_units"
        return {"unit": unit, "direction": first.get("contrast_direction", "unknown"),
                "standardizer": first.get("standardizer", "unknown")}
    b = values[1] if method in ("mean_difference", "cohen_d") else values[2]
    for group in {v["group"] for v in values}:
        roles_for_group = {v.get("group_role", "unknown") for v in values if v["group"] == group} - {"unknown"}
        if len(roles_for_group) > 1:
            raise ValueError("The selected values disagree about their arm roles.")
    roles = [first.get("group_role"), b.get("group_role")]
    order = "intervention" if roles == ["intervention", "comparator"] else "comparator" if roles == ["comparator", "intervention"] else None
    operator = "over" if method in RATIOS else "minus"
    direction = f"{order}_{operator}_{'comparator' if order == 'intervention' else 'intervention'}" if order else "unknown"
    standardizer = "unknown"
    if method in ("mean_difference", "cohen_d"):
        measured = [v for v in values if v["statistic"] != "n"]
        units = [v.get("unit", "unknown") for v in measured]
        if any(unit not in ("unknown", "original_units") for unit in units):
            raise ValueError("Continuous calculations require means and SDs in the same original outcome units.")
        unit = "original_units" if all(u == "original_units" for u in units) else "unknown"
        if method == "cohen_d":
            # Unknown source units cannot certify a standardized result's denominator.
            standardizer = ("change_sd" if first["statistic"] == "mean_change" else "endpoint_sd") if unit != "unknown" else "unknown"
            unit = "standardized_units"
    else:
        unit = "proportion" if method == "risk_difference" else "ratio"
    return {"unit": unit, "direction": direction, "standardizer": standardizer}


def validate_domain(metric, unit, value):
    metric = metric_of(metric)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("The effect must be a finite number.")
    if unit != "unknown" and unit not in UNITS.get(metric, set()):
        raise ValueError("The effect's metric and units are incompatible.")
    if metric == "risk_difference":
        if unit == "unknown":
            raise ValueError("A risk difference needs explicit proportion or percentage-point units before planning.")
        limit = 100 if unit == "percentage_points" else 1
        if abs(value) > limit:
            raise ValueError(f"A risk difference in {unit} must be between {-limit} and {limit}.")
    if metric in RATIOS and value <= 0:
        raise ValueError("A planning ratio must be positive.")


def target_notes(resolved, outcome):
    target = outcome.get("target")
    if not target:
        return ["The outcome's planning metric, units and contrast direction have not been specified."]
    notes = []
    for key, actual in (("metric", metric_of(resolved["metric"])), ("unit", resolved["unit"]), ("direction", resolved["direction"])):
        expected = target.get(key, "unknown")
        if "unknown" in (expected, actual):
            notes.append(f"The planned or selected effect's {key} is not established.")
        elif expected != actual:
            raise ValueError(f"The selected effect's {key} does not match the planned outcome.")
    if metric_of(resolved["metric"]) == "standardized_mean_difference":
        expected, actual = target.get("standardizer", "unknown"), resolved["standardizer"]
        if expected in ("unknown", "other") or actual in ("unknown", "other"):
            notes.append("The SD defining the planned or selected standardized effect is not established.")
        elif expected != actual:
            raise ValueError("The selected effect's standardizing SD does not match the planned outcome.")
    expected_operator = "_over_" if resolved["metric"] in RATIOS else "_minus_"
    if resolved["direction"] != "unknown" and expected_operator not in resolved["direction"]:
        raise ValueError("The selected contrast direction is incompatible with its effect metric.")
    return notes


def importance_notes(resolved, outcome):
    """Only an explicit outcome-specific between-group boundary certifies magnitude.
    Narrative importance evidence remains in the recommendation, with uncertainty visible.
    """
    target = outcome.get("target") or {}
    threshold = target.get("importance_threshold")
    if threshold is None or not target.get("importance_quote", "").strip() or target.get("importance_scope") != "between_groups":
        return ["Clinical importance has not been established for this target by an outcome-specific between-group criterion."]
    if target.get("metric") != metric_of(resolved["metric"]) or target.get("unit") != resolved["unit"] or target.get("direction") != resolved["direction"] or "unknown" in (resolved["direction"], resolved["unit"]):
        return ["The importance criterion's metric, units or direction cannot be matched to the selected effect."]
    if metric_of(resolved["metric"]) == "standardized_mean_difference" and (resolved["standardizer"] not in ("endpoint_sd", "change_sd") or target.get("standardizer") != resolved["standardizer"]):
        return ["The importance criterion's standardizing SD cannot be matched to the selected effect."]
    try:
        validate_domain(resolved["metric"], resolved["unit"], threshold)
    except ValueError:
        return ["The stated importance boundary is invalid on this effect scale."]
    null = 1 if resolved["metric"] in RATIOS else 0
    rule = target.get("importance_rule")
    if not ((rule == "at_least" and threshold > null) or (rule == "at_most" and threshold < null)):
        return ["The stated importance rule must specify a non-null between-group effect boundary."]
    passes = resolved["value"] >= threshold if rule == "at_least" else resolved["value"] <= threshold
    return [] if passes else ["The proposed effect does not meet the investigator's stated important-difference boundary; justify it separately as a planning assumption."]
