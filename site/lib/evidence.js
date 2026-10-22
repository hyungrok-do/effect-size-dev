// What the overlap check and the conclusion are given. A port of python/esf/evidence.py; the tests
// require identical output.
//
// overlapRecords: the extracted records, with what tells reports of one study apart (registration
// numbers, authors, arms and their sizes) and which reviews in the set cite them.
//
// buildEvidence: the candidate values for the conclusion. Every reported value the investigator has
// not excluded (V1, V2, ...), and summaries this code computes across studies (S1, S2, ...): a pooled
// SD where every arm reports its size, and otherwise the median and range. Claude recommends
// candidates by key; every number in a conclusion comes from here, never from the model.

const PAPER = -1; // result_index of a decision about the whole paper
// Statistics that summarize across studies; the rest (n, bounds, test statistics) are only listed.
const SUMMARIZED = new Set([
  'mean', 'sd', 'se', 'median', 'events', 'percent', 'mean_change', 'sd_change', 'mean_difference',
  'standardized_mean_difference', 'odds_ratio', 'risk_ratio', 'hazard_ratio', 'risk_difference', 'icc', 'correlation',
]);
const POOLED = new Set(['sd', 'sd_change']); // pooled across arms and studies, weighted by n - 1

export const recordKey = (paper) => `R${paper.id}`;
const norm = (text) => String(text || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
const extracted = (papers) => papers.filter((p) => p.status === 'extracted' && p.extraction);

export function overlapRecords(papers) {
  const kept = extracted(papers);
  const byPmid = new Map(kept.filter((p) => p.pmid).map((p) => [p.pmid, recordKey(p)]));
  return kept.map((p) => {
    const x = p.extraction;
    const outcomes = new Set(x.results.map((r) => (r.timepoint ? `${r.outcome_measure} at ${r.timepoint}` : r.outcome_measure)));
    return {
      key: recordKey(p),
      source: p.source,
      pmid: p.pmid ?? null,
      nct_ids: p.nct_ids || [],
      doi: p.doi ?? null,
      title: p.title || '',
      authors: p.authors || '',
      year: p.year || '',
      journal: p.journal || '',
      publication_types: p.pubtypes || [],
      design: x.study_design,
      population: x.population,
      groups: x.groups,
      outcomes: [...outcomes].sort(),
      cited_by: (p.cited_by || []).filter((pmid) => byPmid.has(pmid)).map((pmid) => byPmid.get(pmid)),
    };
  });
}

// A result's decision, else its paper's.
function decisionOf(decisions, paperId, index) {
  const mine = decisions[paperId] || {};
  const chosen = mine[index] || mine[PAPER];
  return chosen ? chosen.decision : null;
}

export function buildEvidence(papers, decisions, overlap) {
  const dropped = new Set(); // records the overlap check says not to use
  for (const group of overlap?.groups || []) {
    if (group.use) for (const r of group.records) if (r !== group.use) dropped.add(r);
  }

  const values = [];
  for (const p of extracted(papers)) {
    const checks = p.verification?.values || [];
    p.extraction.results.forEach((r, i) => {
      if (decisionOf(decisions, p.id, i) === 'exclude') return;
      const row = checks[i] || [];
      const sizes = new Map(r.values.filter((v) => v.statistic === 'n').map((v) => [v.group, v.value]));
      r.values.forEach((v, j) => {
        const check = row[j] || null;
        values.push({
          key: `V${values.length + 1}`,
          record: recordKey(p),
          outcome: r.outcome_measure,
          timepoint: r.timepoint,
          group: v.group,
          statistic: v.statistic,
          value: v.value,
          describes: v.describes,
          n: v.statistic === 'n' || !sizes.has(v.group) ? null : sizes.get(v.group),
          relevance: r.relevance,
          confidence: r.confidence,
          quote_found: Boolean(check && check.quote !== 'missing' && check.number_in_quote),
          dropped_for_overlap: dropped.has(recordKey(p)),
        });
      });
    });
  }

  const groups = new Map();
  for (const v of values) {
    if (v.dropped_for_overlap || !SUMMARIZED.has(v.statistic)) continue;
    const key = JSON.stringify([norm(v.outcome), norm(v.timepoint), v.statistic]);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(v);
  }

  const summaries = [];
  for (const group of groups.values()) {
    if (group.length < 2) continue;
    const statistic = group[0].statistic;
    const numbers = group.map((v) => v.value).sort((a, b) => a - b);
    const middle = Math.floor(numbers.length / 2);
    const base = {
      outcome: group[0].outcome,
      timepoint: group[0].timepoint,
      statistic,
      records: [...new Set(group.map((v) => v.record))],
      values: group.map((v) => v.key),
      count: group.length,
      min: numbers[0],
      max: numbers[numbers.length - 1],
    };
    if (POOLED.has(statistic) && group.every((v) => v.n !== null && v.n > 1)) {
      let squares = 0;
      let freedom = 0;
      let total = 0;
      for (const v of group) {
        squares += (v.n - 1) * (v.value * v.value);
        freedom += v.n - 1;
        total += v.n;
      }
      summaries.push({ key: `S${summaries.length + 1}`, method: 'pooled_sd', value: Math.sqrt(squares / freedom), n: total, ...base });
    }
    const median = numbers.length % 2 ? numbers[middle] : (numbers[middle - 1] + numbers[middle]) / 2;
    summaries.push({ key: `S${summaries.length + 1}`, method: 'median', value: median, n: null, ...base });
  }
  return { values, summaries };
}

// What a conclusion was based on, to tell when it is out of date.
export function decisionsBasis(papers, decisions) {
  return extracted(papers).map((p) => [p.id, p.status,
    Object.entries(decisions[p.id] || {}).map(([k, d]) => [Number(k), d.decision]).sort((a, b) => a[0] - b[0])]);
}
