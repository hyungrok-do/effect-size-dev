// CSV and JSON exports, built in the page. Same columns as the Python server's export.

export const PAPER = -1; // result_index of a decision about the whole paper

// A decision on a result wins over a decision on its whole paper.
export const effectiveDecision = (decisions, index) => decisions?.[index] || decisions?.[PAPER] || null;

export const COLUMNS = [
  'paper_id', 'source', 'id', 'doi', 'title', 'year', 'text_used', 'extracted_by', 'result', 'outcome_measure',
  'timepoint', 'analysis_population', 'adjusted', 'relevance', 'confidence', 'group', 'statistic', 'value',
  'describes', 'quote', 'location', 'quote_check', 'number_in_quote', 'concerns', 'flags', 'decision',
  'decision_reason',
];

export function exportRows(papers, decisions, includedOnly) {
  const rows = [];
  for (const paper of papers.filter((p) => p.status === 'extracted' && p.extraction)) {
    const checks = paper.verification?.values || [];
    const flags = paper.verification?.flags || [];
    paper.extraction.results.forEach((result, index) => {
      const decision = effectiveDecision(decisions[paper.id], index);
      if (includedOnly && decision?.decision !== 'include') return;
      result.values.forEach((value, j) => {
        const check = checks[index]?.[j] || {};
        rows.push({
          paper_id: paper.id, source: paper.source, id: paper.ext_id, doi: paper.doi || '', title: paper.title,
          year: paper.year || '', text_used: paper.fulltext_kind || '', extracted_by: paper.extracted_by || '',
          result: index + 1, outcome_measure: result.outcome_measure, timepoint: result.timepoint,
          analysis_population: result.analysis_population, adjusted: result.adjusted, relevance: result.relevance,
          confidence: result.confidence, group: value.group, statistic: value.statistic, value: value.value,
          describes: value.describes, quote: value.quote, location: value.location, quote_check: check.quote ?? '',
          number_in_quote: check.number_in_quote ?? '', concerns: result.concerns.join('; '),
          flags: flags.filter((f) => f.startsWith(`result ${index + 1} `)).join('; '),
          decision: decision?.decision || '', decision_reason: decision?.reason || '',
        });
      });
    });
  }
  return rows;
}

const cell = (v) => {
  const s = v === true ? 'True' : v === false ? 'False' : String(v ?? '');
  return /[",\r\n]/.test(s) ? `"${s.replaceAll('"', '""')}"` : s;
};

export const csvOf = (columns, rows) => [columns.join(','), ...rows.map((r) => columns.map((c) => cell(r[c])).join(','))].join('\r\n') + '\r\n';
export const toCsv = (rows) => csvOf(COLUMNS, rows);

// The agent activity log: one row per finished step or note, oldest first. `job` and `parent` say
// which steps were part of which (Opus's searches while it planned); `models` are the models its
// calls were served by; tokens count input with cache reads and writes.
export const LOG_COLUMNS = [
  'at', 'started', 'agent', 'models', 'step', 'phase', 'job', 'parent', 'paper_id', 'paper_title', 'text', 'detail',
  'ms', 'calls', 'input_tokens', 'output_tokens',
];

export function logRows(events, papers) {
  const titles = new Map(papers.map((p) => [p.id, p.title || p.ext_id]));
  return events.map((e) => ({
    at: e.at, started: e.started || '', agent: e.agent, models: (e.models || []).join('; '), step: e.step, phase: e.phase,
    job: e.job || '', parent: e.parent || '', paper_id: e.paper_id ?? '', paper_title: e.paper_title || titles.get(e.paper_id) || '',
    text: e.text, detail: e.detail || '', ms: e.ms ?? '', calls: e.usage?.calls ?? '', input_tokens: e.usage?.input ?? '',
    output_tokens: e.usage?.output ?? '',
  }));
}

export function download(name, text, type) {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = Object.assign(document.createElement('a'), { href: url, download: name });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
