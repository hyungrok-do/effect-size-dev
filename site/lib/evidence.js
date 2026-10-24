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
// from here, never from the model.

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

export function buildEvidence(papers, decisions, overlap) {
  const useInstead = new Map(); // record -> the record of the same participants to use instead
  for (const group of overlap?.groups || []) {
    if (!group.use || group.relation !== 'same_study') continue;
    for (const r of group.records) if (r !== group.use && !useInstead.has(r)) useInstead.set(r, group.use);
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
          ci_level: v.ci_level ?? null,
          describes: v.describes,
          n: v.statistic === 'n' || !sizes.has(v.group) ? null : sizes.get(v.group),
          relevance: r.relevance,
          confidence: r.confidence,
          quote_found: Boolean(check && check.quote !== 'missing' && check.number_in_quote && check.number_in_source),
          same_participants_as: useInstead.get(recordKey(p)) ?? null,
        });
      });
    });
  }
  const kept = new Set(values.map((v) => v.record));
  return { records: overlapRecords(papers).filter((r) => kept.has(r.key)), values };
}

// What a summary was based on, to tell when it is out of date: each extracted record, its decisions,
// and the values it reports (a re-extraction changes them).
export function decisionsBasis(papers, decisions) {
  return extracted(papers).map((p) => [p.id, p.status,
    Object.entries(decisions[p.id] || {}).map(([k, d]) => [Number(k), d.decision]).sort((a, b) => a[0] - b[0]),
    p.extraction.results.map((r) => [r.outcome_measure, r.timepoint, r.values.map((v) => [v.group, v.statistic, v.value, v.ci_level ?? null])])]);
}
