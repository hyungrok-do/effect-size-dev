// Structured effect checks; keep in step with python/esf/targets.py.
const RATIOS = new Set(['risk_ratio', 'odds_ratio', 'hazard_ratio']);
const UNITS = {
  mean_difference: ['original_units'], standardized_mean_difference: ['standardized_units'],
  risk_difference: ['proportion', 'percentage_points'], risk_ratio: ['ratio'], odds_ratio: ['ratio'], hazard_ratio: ['ratio'],
};
const fail = (message) => { throw new Error(message); };
export const metricOf = (metric) => metric === 'cohen_d' ? 'standardized_mean_difference' : metric;

export function effectDetails(values, method) {
  const first = values[0];
  if (method === 'reported') {
    const metric = first.statistic;
    let unit = first.unit || 'unknown';
    if (unit === 'unknown') {
      if (RATIOS.has(metric)) unit = 'ratio';
      else if (metric === 'standardized_mean_difference') unit = 'standardized_units';
    }
    return { unit, direction: first.contrast_direction || 'unknown', standardizer: first.standardizer || 'unknown' };
  }
  const b = ['mean_difference', 'cohen_d'].includes(method) ? values[1] : values[2];
  for (const group of new Set(values.map((v) => v.group))) {
    const roles = new Set(values.filter((v) => v.group === group).map((v) => v.group_role || 'unknown').filter((r) => r !== 'unknown'));
    if (roles.size > 1) fail('The selected values disagree about their arm roles.');
  }
  const roles = [first.group_role, b.group_role];
  const order = roles[0] === 'intervention' && roles[1] === 'comparator' ? 'intervention'
    : roles[0] === 'comparator' && roles[1] === 'intervention' ? 'comparator' : null;
  const operator = RATIOS.has(method) ? 'over' : 'minus';
  const direction = order ? `${order}_${operator}_${order === 'intervention' ? 'comparator' : 'intervention'}` : 'unknown';
  let unit, standardizer = 'unknown';
  if (['mean_difference', 'cohen_d'].includes(method)) {
    const units = values.filter((v) => v.statistic !== 'n').map((v) => v.unit || 'unknown');
    if (units.some((u) => !['unknown', 'original_units'].includes(u))) fail('Continuous calculations require means and SDs in the same original outcome units.');
    unit = units.every((u) => u === 'original_units') ? 'original_units' : 'unknown';
    if (method === 'cohen_d') {
      standardizer = unit !== 'unknown' ? (first.statistic === 'mean_change' ? 'change_sd' : 'endpoint_sd') : 'unknown';
      unit = 'standardized_units';
    }
  } else unit = method === 'risk_difference' ? 'proportion' : 'ratio';
  return { unit, direction, standardizer };
}

export function validateDomain(metric, unit, value) {
  metric = metricOf(metric);
  if (typeof value !== 'number' || !Number.isFinite(value)) fail('The effect must be a finite number.');
  if (unit !== 'unknown' && !(UNITS[metric] || []).includes(unit)) fail("The effect's metric and units are incompatible.");
  if (metric === 'risk_difference') {
    if (unit === 'unknown') fail('A risk difference needs explicit proportion or percentage-point units before planning.');
    const limit = unit === 'percentage_points' ? 100 : 1;
    if (Math.abs(value) > limit) fail(`A risk difference in ${unit} must be between ${-limit} and ${limit}.`);
  }
  if (RATIOS.has(metric) && value <= 0) fail('A planning ratio must be positive.');
}

export function targetNotes(resolved, outcome) {
  const target = outcome.target;
  if (!target) return ["The outcome's planning metric, units and contrast direction have not been specified."];
  const notes = [];
  for (const [key, actual] of [['metric', metricOf(resolved.metric)], ['unit', resolved.unit], ['direction', resolved.direction]]) {
    const expected = target[key] || 'unknown';
    if ([expected, actual].includes('unknown')) notes.push(`The planned or selected effect's ${key} is not established.`);
    else if (expected !== actual) fail(`The selected effect's ${key} does not match the planned outcome.`);
  }
  if (metricOf(resolved.metric) === 'standardized_mean_difference') {
    const expected = target.standardizer || 'unknown', actual = resolved.standardizer;
    if (['unknown', 'other'].includes(expected) || ['unknown', 'other'].includes(actual)) notes.push('The SD defining the planned or selected standardized effect is not established.');
    else if (expected !== actual) fail("The selected effect's standardizing SD does not match the planned outcome.");
  }
  const operator = RATIOS.has(resolved.metric) ? '_over_' : '_minus_';
  if (resolved.direction !== 'unknown' && !resolved.direction.includes(operator)) fail('The selected contrast direction is incompatible with its effect metric.');
  return notes;
}

export function importanceNotes(resolved, outcome) {
  const target = outcome.target || {}, threshold = target.importance_threshold;
  if (threshold == null || !target.importance_quote?.trim() || target.importance_scope !== 'between_groups') {
    return ['Clinical importance has not been established for this target by an outcome-specific between-group criterion.'];
  }
  if (target.metric !== metricOf(resolved.metric) || target.unit !== resolved.unit || target.direction !== resolved.direction || [resolved.direction, resolved.unit].includes('unknown')) {
    return ["The importance criterion's metric, units or direction cannot be matched to the selected effect."];
  }
  if (metricOf(resolved.metric) === 'standardized_mean_difference' && (!['endpoint_sd', 'change_sd'].includes(resolved.standardizer) || target.standardizer !== resolved.standardizer)) {
    return ["The importance criterion's standardizing SD cannot be matched to the selected effect."];
  }
  try { validateDomain(resolved.metric, resolved.unit, threshold); }
  catch { return ['The stated importance boundary is invalid on this effect scale.']; }
  const nullValue = RATIOS.has(resolved.metric) ? 1 : 0, rule = target.importance_rule;
  if (!((rule === 'at_least' && threshold > nullValue) || (rule === 'at_most' && threshold < nullValue))) {
    return ['The stated importance rule must specify a non-null between-group effect boundary.'];
  }
  const passes = rule === 'at_least' ? resolved.value >= threshold : resolved.value <= threshold;
  return passes ? [] : ["The proposed effect does not meet the investigator's stated important-difference boundary; justify it separately as a planning assumption."];
}
