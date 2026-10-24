// Deterministic checks on an extraction: is each quote in the source, is each number in its quote,
// and are the numbers plausible together. Anything that fails is escalated to the manager.
// A port of python/esf/verify.py; keep the two in step.

const DASHES = /[−‒–—‐‑﹣－]/g;
const NUMBER = /(?<![\d.])-?(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?|\.\d+)(?!\d)/g;
const RATIO_MEASURES = new Set(['odds_ratio', 'risk_ratio', 'hazard_ratio']);

export function normalize(text) {
  return text
    .normalize('NFKC')
    .replace(DASHES, '-')
    .replace(/(?<=\d)[·‧∙](?=\d)/g, '.') // Lancet-style decimal point
    .toLowerCase()
    .split(/\s+/)
    .filter(Boolean)
    .join(' ');
}

// Keep only letters, digits, '.', '-' and '%', so layout differences don't matter.
export const squash = (text) => normalize(text).replace(/[^a-z0-9.%-]/g, '');

export const numbersIn = (text) => [...normalize(text).matchAll(NUMBER)].map((m) => parseFloat(m[0].replaceAll(',', '')));

const same = (a, b) => Math.abs(a - b) <= Math.max(1e-9 * Math.max(Math.abs(a), Math.abs(b)), 1e-12);
const matches = (value) => (n) => same(n, value);
// Python's format(x, 'g'): 6 significant digits, exponent below 1e-4 or from 1e6, no trailing zeros.
export function g(x) {
  const n = Number(x);
  if (n === 0 || !Number.isFinite(n)) return String(n);
  const rounded = Number(n.toPrecision(6));
  const [mantissa, exponent] = rounded.toExponential().split('e');
  const exp = Number(exponent);
  if (exp < -4 || exp >= 6) return `${mantissa}e${exp < 0 ? '-' : '+'}${String(Math.abs(exp)).padStart(2, '0')}`;
  return String(rounded);
}

// Is the value printed in the text, including its sign? Never infer a sign from prose.
export const numberIn = (value, text) => numbersIn(text).some(matches(value));

export class Source {
  constructor(text) {
    this.squashed = squash(text);
    const words = normalize(text).split(' ');
    this.trigrams = new Set();
    for (let i = 0; i + 2 < words.length; i++) this.trigrams.add(words.slice(i, i + 3).join('\u0001'));
    this.numbers = numbersIn(text);
  }

  // 'exact', 'approximate' (most of the quote's word triples occur in the source) or 'missing'.
  findQuote(quote) {
    const target = squash(quote);
    if (target && this.squashed.includes(target)) return 'exact';
    const words = normalize(quote).split(' ');
    const grams = [];
    for (let i = 0; i + 2 < words.length; i++) grams.push(words.slice(i, i + 3).join('\u0001'));
    if (grams.length && grams.filter((x) => this.trigrams.has(x)).length / grams.length >= 0.8) return 'approximate';
    return 'missing';
  }

  hasNumber(value) {
    return this.numbers.some(matches(value));
  }
}

export function checkOk(check, quotesReliable) {
  if (!check.number_in_quote || !check.number_in_source) return false;
  if (check.quote !== 'missing') return true;
  // A PDF's text layer often scrambles tables; then accept a number that occurs in the text.
  return !quotesReliable && check.number_in_source;
}

export function plausibility(result) {
  const flags = [];
  const byGroup = new Map();
  const stat = (group, name) => byGroup.get(group)?.get(name) || [];
  for (const v of result.values) {
    if (!byGroup.has(v.group)) byGroup.set(v.group, new Map());
    const stats = byGroup.get(v.group);
    stats.set(v.statistic, [...(stats.get(v.statistic) || []), v.value]);
    if (v.statistic === 'p_value' && !(v.value >= 0 && v.value <= 1)) flags.push(`p-value ${g(v.value)} outside [0, 1]`);
    if (RATIO_MEASURES.has(v.statistic) && v.value <= 0) flags.push(`${v.statistic.replaceAll('_', ' ')} ${g(v.value)} is not positive`);
    if (['sd', 'se', 'sd_change'].includes(v.statistic) && v.value < 0) flags.push(`negative ${v.statistic.toUpperCase()} (${g(v.value)}) for ${v.group}`);
    if (v.statistic === 'percent' && !(v.value >= 0 && v.value <= 100)) flags.push(`percent ${g(v.value)} outside [0, 100] for ${v.group}`);
  }
  for (const group of byGroup.keys()) {
    const [lows, highs] = [stat(group, 'ci_lower'), stat(group, 'ci_upper')];
    if (lows.length === 1 && highs.length === 1 && lows[0] > highs[0]) flags.push(`CI lower bound above upper bound for ${group}`);
    const [ns, events] = [stat(group, 'n'), stat(group, 'events')];
    if (ns.length === 1 && events.length === 1 && events[0] > ns[0]) flags.push(`events exceed n for ${group}`);
    const [sds, ses] = [stat(group, 'sd'), stat(group, 'se')];
    if (ns.length === 1 && sds.length === 1 && ses.length === 1 && ns[0] > 1) {
      const implied = sds[0] / Math.sqrt(ns[0]);
      if (Math.abs(ses[0] - implied) > 0.15 * Math.max(Math.abs(ses[0]), Math.abs(implied))) {
        flags.push(`SD and SE for ${group} disagree given n = ${g(ns[0])}`);
      }
    }
  }
  const sdValues = [...byGroup.keys()].map((k) => stat(k, 'sd')).filter((s) => s.length === 1 && s[0] > 0).map((s) => s[0]);
  if (sdValues.length >= 2 && Math.max(...sdValues) / Math.min(...sdValues) > 3) {
    flags.push('SDs differ more than threefold between groups; one may be an SE');
  }
  const groups = [...byGroup.values()];
  if (groups.some((s) => s.has('mean') || s.has('mean_change')) && !groups.some((s) => s.has('n'))) {
    flags.push('means reported without group sizes');
  }
  return flags;
}

export function verify(extraction, sourceText, quotesReliable = true) {
  const source = new Source(sourceText);
  const report = { values: [], flags: [] };
  extraction.results.forEach((result, index) => {
    const checks = [];
    for (const v of result.values) {
      const check = {
        quote: source.findQuote(v.quote),
        number_in_quote: numberIn(v.value, v.quote),
        number_in_source: source.hasNumber(v.value),
      };
      checks.push(check);
      if (!checkOk(check, quotesReliable)) {
        let problem = 'was not found in the source';
        if (!check.number_in_quote) problem = 'is not in its quote';
        else if (check.number_in_source) problem = 'has a quote not found in the source';
        report.flags.push(`result ${index + 1} (${result.outcome_measure}): ${v.statistic} ${g(v.value)} for ${v.group} ${problem}`);
      }
    }
    report.values.push(checks);
    for (const f of plausibility(result)) report.flags.push(`result ${index + 1} (${result.outcome_measure}): ${f}`);
  });
  return report;
}

// Why a worker extraction should go to the manager; empty if it can stand.
export function escalationReasons(extraction, report) {
  if (!extraction || !report) return ['the worker could not complete the extraction'];
  const reasons = [...report.flags];
  extraction.results.forEach((result, index) => {
    if (result.confidence === 'low') reasons.push(`result ${index + 1}: the worker reported low confidence`);
    for (const c of result.concerns) reasons.push(`result ${index + 1}: worker concern: ${c}`);
  });
  return reasons;
}

// Deterministic, and the same as the Python version: a paper is always in or out of the audit sample.
export async function inAuditSample(key, fraction) {
  const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(key)));
  const first = ((digest[0] << 24) >>> 0) + (digest[1] << 16) + (digest[2] << 8) + digest[3];
  return first / 2 ** 32 < fraction;
}
