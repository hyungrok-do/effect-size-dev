// What the overlap check and the summary are given. A port of python/esf/evidence.py; the tests
// require identical output.
//
// overlapRecords: the extracted records, with what tells reports of one study apart (registration
// numbers, authors, arms and their sizes) and which reviews in the set cite them.
//
// buildEvidence: what the summary describes: the records the investigator kept something from, and
// every reported value they have not excluded (V1, V2, ...). A value from a report of a study that is
// reported more than once (a same_study group) is marked with the record whose numbers to use
// instead; reviews and the studies they pool are separate studies and are not marked. Nothing is
// combined across studies. Claude refers to values by key, so every number in a summary comes
// from here, never from the model. Where the manager has read a paper's statistical context, its kept
// results carry their estimands (E1, E2, ...), with whether the quote of each one's model was found in
// the paper, each value names its result's, and its record carries the design and the paper's own
// sample-size calculation as the manager stated them.

const PAPER = -1; // result_index of a decision about the whole paper

export const recordKey = (paper) => `R${paper.id}`;
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

// The manager's reading of a paper's statistical context (a StudyContext), when it has one.
const contextOf = (paper) => (paper.context?.status === 'done' ? paper.context.result : null);

// The manager's reading of each result, by its number (from 1), with the check of its model's quote; the
// first reading of a number when there are more, as the review and the exports take it.
function readings(paper) {
  const found = paper.context?.checks?.results || [];
  const read = new Map();
  (contextOf(paper)?.results || []).forEach((c, k) => {
    if (!read.has(c.result)) read.set(c.result, [c, found[k] ?? null]);
  });
  return read;
}

// What the summary is told about a result's estimand, from the manager's reading (ResultContext).
const ESTIMAND_FIELDS = [
  'population', 'treatments', 'variable', 'intercurrent_events', 'summary_measure', 'model', 'missing_data',
  'numbers_are', 'vs_plan', 'differences', 'use',
];

// The manager's statement of a record's design and its own sample-size calculation, if it read them.
function recordContext(paper) {
  const context = contextOf(paper);
  if (!context) return null;
  return {
    design: context.design || '', sample_size: context.sample_size || '',
    sample_size_found: ['exact', 'approximate'].includes(paper.context.checks?.sample_size), notes: context.notes || '',
  };
}

export function buildEvidence(papers, decisions, overlap) {
  const useInstead = new Map(); // record -> the record of the same participants to use instead
  for (const group of overlap?.groups || []) {
    if (!group.use || group.relation !== 'same_study') continue;
    for (const r of group.records) if (r !== group.use && !useInstead.has(r)) useInstead.set(r, group.use);
  }

  const values = [];
  const estimands = [];
  for (const p of extracted(papers)) {
    const checks = p.verification?.values || [];
    const read = readings(p);
    p.extraction.results.forEach((r, i) => {
      if (decisionOf(decisions, p.id, i) === 'exclude') return;
      const row = checks[i] || [];
      const sizes = new Map(r.values.filter((v) => v.statistic === 'n').map((v) => [v.group, v.value]));
      let estimand = null;
      if (read.has(i + 1)) {
        estimand = `E${estimands.length + 1}`;
        const [c, modelQuote] = read.get(i + 1);
        estimands.push({
          key: estimand, record: recordKey(p), result: `${recordKey(p)}:${i}`, outcome: r.outcome_measure, timepoint: r.timepoint,
          ...Object.fromEntries(ESTIMAND_FIELDS.map((name) => [name, c[name] ?? null])),
          model_quote: modelQuote, // 'exact', 'approximate', 'missing', or null with no quote
        });
      }
      r.values.forEach((v, j) => {
        const check = row[j] || null;
        values.push({
          key: `V${values.length + 1}`,
          record: recordKey(p),
          result: `${recordKey(p)}:${i}`,
          outcome: r.outcome_measure,
          timepoint: r.timepoint,
          group: v.group,
          statistic: v.statistic,
          value: v.value,
          ci_level: v.ci_level ?? null,
          describes: v.describes,
          n: v.statistic === 'n' || !sizes.has(v.group) ? null : sizes.get(v.group),
          relevance: r.relevance,
          confidence: r.confidence,
          decision: decisionOf(decisions, p.id, i),
          analysis_population: r.analysis_population,
          adjusted: r.adjusted,
          outcome_type: r.outcome_type ?? 'unclear',
          independent_groups: r.independent_groups ?? null,
          concerns: r.concerns || [],
          flags: p.verification?.flags || [],
          quote: v.quote,
          location: v.location,
          quote_found: Boolean(check && check.quote !== 'missing' && check.number_in_quote && check.number_in_source),
          same_participants_as: useInstead.get(recordKey(p)) ?? null,
          estimand,
        });
      });
    });
  }
  const kept = new Set(values.map((v) => v.record));
  for (const p of extracted(papers)) {
    if (p.extraction.appraisal?.length && (p.extraction.results.some((_, i) => decisionOf(decisions, p.id, i) !== 'exclude')
      || (!p.extraction.results.length && decisionOf(decisions, p.id, PAPER) !== 'exclude'))) kept.add(recordKey(p));
  }
  const records = overlapRecords(papers).filter((r) => kept.has(r.key));
  const byKey = new Map(papers.map((p) => [recordKey(p), p]));
  for (const record of records) {
    const paper = byKey.get(record.key);
    const x = paper.extraction;
    Object.assign(record, {
      fulltext_kind: paper.fulltext_kind ?? null, study_type: paper.screen?.study_type ?? null,
      matches_spec: x.matches_spec ?? false, mismatch_reasons: x.mismatch_reasons || [], notes: x.notes || '',
      appraisal: (x.appraisal || []).map((a, i) => ({ ...a, quote_found: ['exact', 'approximate'].includes(paper.verification?.appraisal?.[i]) })),
      context: recordContext(paper),
    });
  }
  return { records, estimands: estimands.filter((e) => kept.has(e.record)), values };
}

// What a summary was based on, to tell when it is out of date: each extracted record, its decisions,
// and the values it reports (a re-extraction changes them), and the manager's reading of its
// statistical context once there is one.
export function decisionsBasis(papers, decisions) {
  return structuredClone(extracted(papers).map((p) => {
    const row = [p.id, p.status,
      Object.entries(decisions[p.id] || {}).map(([k, d]) => [Number(k), d.decision]).sort((a, b) => a[0] - b[0]),
      p.extraction.results.map((r) => [r.outcome_measure, r.timepoint, r.values.map((v) => [v.group, v.statistic, v.value, v.ci_level ?? null])]),
      p.extraction, p.verification ?? null, p.fulltext_kind ?? null, p.screen ?? null];
    if (contextOf(p)) row.push(contextOf(p)); // a summary written before then goes out of date when it is read
    return row;
  }));
}
