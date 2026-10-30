// Fixed, auditable within-study calculations. Keep in step with python/esf/planning.py.
const EFFECTS = new Set(['mean_difference', 'standardized_mean_difference', 'risk_difference', 'risk_ratio', 'odds_ratio', 'hazard_ratio']);
const RATIOS = new Set(['risk_ratio', 'odds_ratio', 'hazard_ratio']);
export const PLANNING_VERSION = 1;
const fail = (message) => { throw new Error(message); };
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

export function resolveChoice(choice, evidence) {
  const method = choice?.method, keys = choice?.inputs || [];
  const counts = { reported: 1, mean_difference: 2, cohen_d: 6, risk_difference: 4, risk_ratio: 4, odds_ratio: 4 };
  if (!(method in counts) || keys.length !== counts[method] || new Set(keys).size !== keys.length) fail('Wrong method, input count or duplicate input keys.');
  const lookup = new Map(evidence.values.map((v) => [v.key, v]));
  if (keys.some((k) => !lookup.has(k))) fail('A selected value is not in the current evidence.');
  const vs = keys.map((k) => lookup.get(k));
  if (vs.some((v) => !v.quote_found || v.flags?.length || v.decision === 'exclude')) fail('Selected inputs must have verified quotes and no unresolved verification flags.');
  if (vs.some((v) => typeof v.value !== 'number' || !Number.isFinite(v.value))) fail('Selected inputs must be finite numbers.');
  if (new Set(vs.map((v) => v.result)).size !== 1 || !vs[0].result) fail('Inputs must come from the same outcome, visit and analysis result in one report.');
  const first = vs[0], nums = vs.map((v) => v.value);
  let metric = method, scale = 'original outcome units', contrast = first.group;
  let formula = 'Reported estimate (no numerical transformation)', value;
  if (method === 'reported') {
    metric = first.statistic;
    if (!EFFECTS.has(metric)) fail('A nuisance parameter or CI bound cannot serve as the final treatment effect.');
    value = nums[0];
    if (RATIOS.has(metric) && value <= 0) fail('A reported ratio must be positive.');
    scale = first.describes;
  } else {
    if (vs.some((v) => v.independent_groups !== true || v.adjusted !== 'unadjusted')) fail('Derived contrasts require unadjusted data from independent parallel groups.');
    const record = evidence.records.find((r) => r.key === first.record) || {};
    if (/cluster|cross.?over|paired|matched/i.test(record.design || '')) fail('This design requires a reported design-appropriate contrast.');
    const stats = vs.map((v) => v.statistic);
    let a, b;
    if (['mean_difference', 'cohen_d'].includes(method)) {
      if (first.outcome_type !== 'continuous' || !(same(stats.slice(0, 2), ['mean', 'mean']) || same(stats.slice(0, 2), ['mean_change', 'mean_change']))) fail('Use compatible continuous endpoint means or two change means.');
      [a, b] = [vs[0].group, vs[1].group];
      value = nums[0] - nums[1];
      formula = 'mean_A - mean_B';
      if (method === 'cohen_d') {
        const sd = stats[0] === 'mean_change' ? 'sd_change' : 'sd';
        if (!same(stats.slice(2), [sd, sd, 'n', 'n']) || !same(vs.slice(2).map((v) => v.group), [a, b, a, b])) fail('SDs and sample sizes must match their arms and endpoint/change metric.');
        const [sa, sb, na, nb] = nums.slice(2);
        if (Math.min(sa, sb) <= 0 || Math.min(na, nb) <= 1 || !Number.isInteger(na) || !Number.isInteger(nb)) fail('Cohen d requires positive SDs and integer arm sizes greater than one.');
        value /= Math.sqrt(((na - 1) * sa ** 2 + (nb - 1) * sb ** 2) / (na + nb - 2));
        formula = '(mean_A - mean_B) / sqrt(((n_A - 1)*sd_A^2 + (n_B - 1)*sd_B^2)/(n_A + n_B - 2))';
        scale = 'pooled within-study SD units (Cohen d)';
      }
      contrast = `${a} minus ${b}`;
    } else {
      if (first.outcome_type !== 'binary' || !same(stats, ['events', 'n', 'events', 'n'])) fail('Binary contrasts need participants with an event and their analyzed denominators.');
      const [ea, na, eb, nb] = nums;
      [a, b] = [vs[0].group, vs[2].group];
      if (!same(vs.map((v) => v.group), [a, a, b, b]) || Math.min(na, nb) <= 0) fail('Binary counts and denominators must match their arms.');
      if (nums.some((n) => !Number.isInteger(n)) || !(ea >= 0 && ea <= na && eb >= 0 && eb <= nb)) fail('Binary counts must be integers between zero and the arm size.');
      const pa = ea / na, pb = eb / nb;
      if (method === 'risk_difference') {
        value = pa - pb; formula = 'events_A/n_A - events_B/n_B'; scale = 'proportion difference (not percent)'; contrast = `${a} minus ${b}`;
      } else if (method === 'risk_ratio') {
        if (pb === 0) fail('Risk ratio is undefined with zero comparator events; no correction is added.');
        value = pa / pb; formula = '(events_A/n_A) / (events_B/n_B)'; scale = 'risk ratio'; contrast = `${a} over ${b}`;
      } else {
        if (ea === na || eb === 0) fail('Odds ratio is undefined for these cells; no correction is added.');
        value = ea * (nb - eb) / ((na - ea) * eb);
        formula = 'events_A*(n_B-events_B) / ((n_A-events_A)*events_B)'; scale = 'odds ratio'; contrast = `${a} over ${b}`;
      }
    }
    if (a === b) fail('Two distinct arms are required.');
  }
  if (!Number.isFinite(value)) fail('The derived effect is not finite.');
  const basis_value = value, target = choice.target_value;
  if (target != null) {
    if (typeof target !== 'number' || !Number.isFinite(target) || !choice.rationale?.trim()) fail('A proposed target needs a finite value and explicit justification.');
    const nullValue = RATIOS.has(metric) ? 1 : 0;
    if ((target - nullValue) * (basis_value - nullValue) <= 0 || (RATIOS.has(metric) && target <= 0)) fail("A proposed target must retain the evidence estimate's direction and valid scale.");
    if (method === 'risk_difference' && Math.abs(target) > 1) fail('A proposed risk difference must be between minus one and one.');
    value = target;
  }
  return { value, basis_value, origin: target != null ? 'planning_assumption' : 'evidence_estimate',
    metric, scale, contrast, outcome: first.outcome, timepoint: first.timepoint, record: first.record,
    formula, inputs: keys, input_values: nums };
}

// What the manager's reading of the selected result's estimand leaves open: no reading, or an estimand
// that differs from the planned one.
function estimandNotes(key, evidence) {
  const reading = (evidence.estimands || []).find((e) => e.key === key);
  if (!reading) return ["The manager has not read the selected result's estimand and model."];
  if (reading.vs_plan === 'different') {
    const how = (reading.differences || []).join('; ');
    return [`The manager read the selected result's estimand as different from the planned one${how ? `: ${how}.` : '.'}`];
  }
  return [];
}

const empty = (outcome) => ({ outcome, status: 'insufficient_evidence', estimand: '', choice: null, resolved: null,
  justification: 'The head agent did not provide a defensible effect for this outcome.', clinical_importance: 'unknown',
  importance_basis: '', realism: '', evidence_quality: '', assumptions: [], sensitivity: [], nuisance_parameters: [],
  gaps: ['A source-supported planning recommendation is required.'], validation_notes: [] });

export function finalizeSummary(raw, evidence, spec) {
  const result = structuredClone(raw);
  const known = new Map(evidence.values.map((v) => [v.key, v]));
  const records = new Map(evidence.records.map((r) => [r.key, r]));
  const context = spec.planning || {};
  const outcomes = [...(spec.outcomes || [])].sort((a, b) => Number(a.role !== 'primary') - Number(b.role !== 'primary'));
  result.recommendations = outcomes.map((outcome) => {
    const matches = (raw.recommendations || []).filter((r) => r.outcome === outcome.name);
    if (matches.length !== 1) return empty(outcome.name);
    const rec = structuredClone(matches[0]);
    rec.resolved = null; rec.validation_notes = [];
    try {
      if (rec.status === 'insufficient_evidence') { rec.choice = null; rec.sensitivity = []; return rec; }
      if (!rec.choice || ['estimand', 'justification', 'realism', 'evidence_quality', 'importance_basis'].some((k) => !rec[k]?.trim())) fail('A numerical recommendation needs an estimand and full justification.');
      const narrative = JSON.stringify(rec);
      if ([...narrative.matchAll(/\[(V\d+)\]/g)].some((m) => !known.has(m[1])) || (narrative.match(/\bR\d+\b/g) || []).some((key) => !records.has(key))) fail('The recommendation cites a value or study outside the current evidence.');
      const resolved = resolveChoice(rec.choice, evidence);
      if (resolved.value === (RATIOS.has(resolved.metric) ? 1 : 0)) fail('The null effect cannot be the final detectable target difference.');
      if (/non[\s-]*inferior|equivalence/i.test(context.objective || '')) fail('A noninferiority/equivalence margin needs separate clinical justification; it is not a selected observed effect.');
      rec.resolved = resolved;
      const vs = resolved.inputs.map((k) => known.get(k)), source = records.get(resolved.record);
      const notes = rec.validation_notes;
      const supplied = Boolean(context.clinical_importance?.trim() && context.clinical_importance_source?.trim());
      const cited = new Set([resolved.record, ...(rec.importance_basis.match(/\bR\d+\b/g) || [])]);
      const extracted = [...cited].some((key) => (records.get(key)?.appraisal || []).some((a) => a.domain === 'clinical_importance' && a.quote_found));
      if (rec.clinical_importance !== 'supported' || !(supplied || extracted)) {
        rec.clinical_importance = 'unknown'; notes.push('Clinical importance has not been established from the supplied evidence.');
      }
      if (['objective', 'effect_measure', 'analysis', 'analysis_population', 'intercurrent_events'].some((k) => !context[k]?.trim())) notes.push('The planned estimand or analysis still has unspecified components.');
      if (vs.some((v) => v.decision !== 'include')) notes.push('Some selected results have not yet been included by the investigator.');
      if (vs.some((v) => v.relevance !== 'direct' || v.confidence !== 'high' || v.concerns?.length)) notes.push('Selected evidence has indirectness or extraction concerns.');
      const domains = new Set((source.appraisal || []).filter((a) => a.quote_found).map((a) => a.domain));
      if (!source.fulltext_kind || source.fulltext_kind === 'abstract' || ['randomization', 'deviations', 'missing_outcomes', 'measurement', 'selective_reporting'].some((d) => !domains.has(d))) notes.push('Full methodological appraisal is unavailable for the selected report.');
      const normalize = (s) => s.toLowerCase().split(/\s+/).filter(Boolean).join(' ');
      if (![outcome.name, outcome.measure || ''].map(normalize).includes(normalize(resolved.outcome)) || normalize(resolved.timepoint) !== normalize(outcome.timepoint || '')) notes.push('The source outcome label or timepoint differs from the planned outcome; confirm equivalence.');
      if (source.study_type !== 'rct' || !source.matches_spec) notes.push('The selected report is not a confirmed matching randomized trial; assess applicability.');
      notes.push(...estimandNotes(vs[0].estimand, evidence));
      if (notes.length) rec.status = 'provisional';
      rec.sensitivity = (rec.sensitivity || []).flatMap((scenario) => {
        try {
          const alt = resolveChoice(scenario.choice, evidence);
          if (['metric', 'scale', 'contrast', 'outcome', 'timepoint'].some((k) => alt[k] !== resolved[k])) fail('Sensitivity scenarios must match the effect metric, scale, contrast, outcome and timepoint.');
          return [{ ...scenario, resolved: alt }];
        } catch (error) { notes.push(`Sensitivity scenario omitted: ${error.message}`); return []; }
      });
    } catch (error) {
      rec.status = 'insufficient_evidence'; rec.choice = null; rec.resolved = null; rec.sensitivity = [];
      rec.validation_notes.push(error.message);
    }
    return rec;
  });
  result.planning_version = PLANNING_VERSION;
  return result;
}
