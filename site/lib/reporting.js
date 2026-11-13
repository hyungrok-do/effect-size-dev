// Stage-one tables; parity with python/esf/reporting.py. No synthesis is calculated.
import { Source } from './verify.js';
export const VERSION = 1;
export const STATUSES = {
  direct_synthesis: 'DIRECT SYNTHESIS', transform_then_synthesize: 'TRANSFORM THEN SYNTHESIZE',
  assumption_dependent: 'ASSUMPTION-DEPENDENT', insufficient_information: 'INSUFFICIENT INFORMATION', keep_separate: 'KEEP SEPARATE',
};
const CRITICAL = ['population.intended', 'population.eligibility', 'population.analysis_set', 'population.subgroup', 'population.setting',
  'formulation.treatment', 'formulation.comparator', 'formulation.outcome', 'formulation.time_horizon',
  'formulation.intercurrent_events', 'target.reported_parameter', 'target.conditioning', 'target.scale', 'target.units', 'target.direction', 'target.interpretation', 'target.coefficient_mapping'];
export const facts = (profile) => Object.entries(profile || {}).flatMap(([section, fields]) =>
  Object.entries(fields).map(([name, fact]) => ({ field: `${section}.${name}`, ...fact })));
export const factValue = (profile, path) => { const [section, name] = path.split('.'); return profile?.[section]?.[name]?.value || 'Not reported'; };
export function profileChecks(context, text, count, audit = null) {
  const source = new Source(text);
  return Array.from({ length: count }, (_, i) => {
    const number = i + 1, readings = (context.results || []).filter((r) => r.result === number);
    const reviews = (audit?.results || []).filter((r) => r.result === number);
    const profile = readings.length === 1 ? readings[0].profile : null;
    const flags = profile ? [] : ['Structured population/formulation/parameter reading unavailable'];
    const checked = facts(profile).map((fact) => {
      const found = fact.quote?.trim() ? source.findQuote(fact.quote) : null;
      let invalid = ['reported', 'reconstructed'].includes(fact.status) && (!fact.value?.trim() || !fact.location?.trim() || !['exact', 'approximate'].includes(found));
      if (fact.status === 'reconstructed' && !fact.rationale?.trim()) invalid = true;
      if (fact.status === 'not_reported' && fact.value?.trim()) invalid = true;
      if (invalid) flags.push(`${fact.field}: unsupported field or missing derivation`);
      return { ...fact, quote_check: found, supported: !invalid && ['reported', 'reconstructed'].includes(fact.status) };
    });
    if (reviews.length !== 1) flags.push('Independent semantic audit unavailable or incomplete');
    else if (reviews[0].verdict !== 'supported' || reviews[0].issues?.length) flags.push(...(reviews[0].issues?.length ? reviews[0].issues : ['Source information remains insufficient']));
    return { result: number, status: flags.length ? 'needs_attention' : 'verified', flags, facts: checked };
  });
}
const decision = (decisions, id, i) => (decisions[id]?.[i] || decisions[id]?.[-1])?.decision ?? null;
export function analysisRows(papers, decisions) {
  return papers.filter((p) => p.status === 'extracted' && p.extraction).flatMap((paper) => {
    const context = paper.context || {}, readings = context.status === 'done' ? context.result.results : [];
    return paper.extraction.results.flatMap((result, index) => {
      if (decision(decisions, paper.id, index) === 'exclude') return [];
      const reading = readings.find((r) => r.result === index + 1) || {};
      const check = (context.profile_checks || []).find((r) => r.result === index + 1) || {};
      return [{ id: `R${paper.id}:${index}`, record: `R${paper.id}`, title: paper.title || paper.ext_id,
        outcome: result.outcome_measure, timepoint: result.timepoint, population: reading.population || paper.extraction.population || 'Not reported',
        analysis_set: result.analysis_population, adjusted: result.adjusted, profile: reading.profile ?? null,
        verification: check.status || 'needs_attention', flags: check.flags || ['Structured reading and independent audit required'],
        facts: check.facts || facts(reading.profile), source_type: paper.fulltext_kind ?? null, decision: decision(decisions, paper.id, index) }];
    });
  });
}
export function finalizeTables(plan, evidence, overlap = null) {
  const analyses = structuredClone(evidence.analyses || []), byId = new Map(analyses.map((a) => [a.id, a]));
  const values = evidence.values || [];
  for (const a of analyses) a.values = values.filter((v) => v.result === a.id).map((v) => v.key);
  const groups = [], used = new Set(), warnings = [];
  for (const proposed of plan?.groups || []) {
    const rawIds = proposed.analyses || [], ids = [...new Set(rawIds.filter((i) => byId.has(i) && !used.has(i)))];
    if (!ids.length) { warnings.push('Empty, unknown or repeated analysis group omitted'); continue; }
    const invalidIds = ids.length !== rawIds.length;
    ids.forEach((i) => used.add(i));
    const group = structuredClone(proposed), members = ids.map((i) => byId.get(i)), flags = [];
    if (invalidIds) flags.push('Unknown or repeated analysis IDs were removed');
    const records = [...new Set(members.map((a) => a.record))];
    if (records.length < 2) flags.push('Fewer than two independent study reports; no cross-study synthesis');
    if (members.some((a) => a.verification !== 'verified')) flags.push('Structured extraction or independent semantic audit needs attention');
    const vs = values.filter((v) => ids.includes(v.result));
    if (!vs.length || vs.some((v) => !v.quote_found || v.flags?.length)) flags.push('Reported values are missing or have unresolved source/numerical checks');
    if (records.some((r) => members.filter((a) => a.record === r).length > 1)) flags.push('Repeated analyses within a study require covariance or an explicit selection rule');
    for (const related of overlap?.groups || []) if (records.filter((r) => related.records.includes(r)).length > 1) flags.push(`Study overlap/dependence unresolved: ${related.relation}`);
    let status = group.status || 'insufficient_information';
    if (!Object.hasOwn(STATUSES, status)) { status = 'insufficient_information'; flags.push('Unknown combination status'); }
    if (!group.reason?.trim() || !group.dependence?.trim()) flags.push('Compatibility reasoning or dependence assessment missing');
    if (status === 'direct_synthesis') {
      const declared = { population: 'population.intended', outcome: 'formulation.outcome', timepoint: 'formulation.time_horizon', parameter: 'target.reported_parameter' };
      for (const [label, path] of Object.entries(declared)) if (members.some((a) => (group[label] || '').trim().toLowerCase() !== factValue(a.profile, path).trim().toLowerCase())) flags.push(`Requested ${label} differs from the source definition; a mapping is required`);
      for (const path of CRITICAL) {
        const vals = members.map((a) => factValue(a.profile, path).trim().toLowerCase());
        const supporting = members.map((a) => a.facts.find((f) => f.field === path) || {});
        if (new Set(vals).size !== 1 || supporting.some((f) => !f.supported)) flags.push(`${path}: definitions differ or are unresolved`);
      }
      if (group.inputs_available !== 'yes' || group.mapping?.trim().toLowerCase() !== 'identity') flags.push('Direct synthesis requires an identity mapping and available inputs');
      if (members.some((a) => /\bconditional\b/i.test(factValue(a.profile, 'target.conditioning')))) {
        const adjusted = members.map((a) => factValue(a.profile, 'formulation.adjustment').trim().toLowerCase());
        if (new Set(adjusted).size !== 1 || adjusted.includes('not reported')) flags.push('Conditional parameters have differing or unknown adjustment sets');
      }
      for (const a of members) {
        const native = vs.filter((v) => v.result === a.id && ['mean_difference', 'standardized_mean_difference', 'odds_ratio', 'risk_ratio', 'risk_difference', 'hazard_ratio', 'coefficient'].includes(v.statistic));
        let usable = false;
        if (native.length === 1) {
          const same = vs.filter((v) => v.result === a.id && v.group === native[0].group);
          const ses = same.filter((v) => v.statistic === 'se' && v.value > 0), low = same.filter((v) => v.statistic === 'ci_lower'), high = same.filter((v) => v.statistic === 'ci_upper');
          usable = ses.length === 1 || (low.length === 1 && high.length === 1 && low[0].ci_level != null && low[0].ci_level === high[0].ci_level && low[0].value < high[0].value);
        }
        if (!usable) flags.push(`${a.id}: native effect and usable uncertainty must be identified`);
      }
    }
    if (['transform_then_synthesize', 'assumption_dependent'].includes(status)) {
      if (!group.mapping?.trim() || !group.methodological_sources?.length || !group.required_inputs?.length) flags.push('Mapping, methodological reference or required inputs missing');
      if (status === 'transform_then_synthesize' && group.inputs_available !== 'yes') status = 'assumption_dependent';
      if (status === 'assumption_dependent' && !group.assumptions?.length) flags.push('Approximation/transport assumptions and sensitivity requirements missing');
    }
    if (flags.length && status !== 'keep_separate') status = 'insufficient_information';
    groups.push({ ...group, id: `G${groups.length + 1}`, analyses: ids, records, status, validation_notes: [...new Set(flags)],
      values: vs.map((v) => v.key), combined_effect: null, combined_uncertainty: null, calculation_status: 'Not calculated — stage-two blueprint' });
  }
  for (const a of analyses) {
    if (used.has(a.id)) continue;
    groups.push({ id: `G${groups.length + 1}`, analyses: [a.id], records: [a.record], population: a.population,
      comparison: `${factValue(a.profile, 'formulation.treatment')} vs ${factValue(a.profile, 'formulation.comparator')}`,
      outcome: a.outcome, timepoint: a.timepoint, parameter: factValue(a.profile, 'target.reported_parameter'),
      status: 'insufficient_information', reason: 'No verified multi-study grouping provided', differences: [], mapping: '', required_inputs: [],
      assumptions: [], methodological_sources: [], inputs_available: 'unknown', dependence: 'Not assessed', validation_notes: a.flags,
      values: a.values, combined_effect: null, combined_uncertainty: null, calculation_status: 'Not calculated — stage-two blueprint' });
  }
  const findings = [], partitions = new Map();
  for (const a of analyses) {
    const p = a.profile, vals = CRITICAL.map((path) => factValue(p, path).trim().toLowerCase());
    const uncertain = CRITICAL.some((path) => !a.facts.find((f) => f.field === path)?.supported);
    if (/\bconditional\b/i.test(factValue(p, 'target.conditioning'))) vals.push(factValue(p, 'formulation.adjustment').trim().toLowerCase());
    const signature = JSON.stringify([...vals, ...(uncertain ? [a.id] : [])]);
    if (!partitions.has(signature)) {
      partitions.set(signature, findings.length);
      findings.push({ id: `P${findings.length + 1}`, population: p ? factValue(p, 'population.intended') : a.population,
        analysis_set: p ? factValue(p, 'population.analysis_set') : a.analysis_set, subgroup: factValue(p, 'population.subgroup'),
        eligibility: factValue(p, 'population.eligibility'), setting: factValue(p, 'population.setting'),
        conditioning: factValue(p, 'target.conditioning'), scale: factValue(p, 'target.scale'),
        units: factValue(p, 'target.units'), direction: factValue(p, 'target.direction'),
        comparison: `${factValue(p, 'formulation.treatment')} vs ${factValue(p, 'formulation.comparator')}`,
        outcome: a.outcome, timepoint: a.timepoint, parameter: factValue(p, 'target.reported_parameter'), analyses: [], records: [], groups: [], values: [] });
    }
    const row = findings[partitions.get(signature)];
    row.analyses.push(a.id); row.values.push(...a.values); row.records = [...new Set([...row.records, a.record])];
    row.groups = [...new Set([...row.groups, ...groups.filter((g) => g.analyses.includes(a.id)).map((g) => g.id)])];
  }
  return { version: VERSION, analyses, findings, groups, warnings, statuses: STATUSES };
}
