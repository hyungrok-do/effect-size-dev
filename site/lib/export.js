// CSV and JSON exports, built in the page. Same columns as the Python server's export.

import { roleOf } from './providers.js';

export const PAPER = -1; // result_index of a decision about the whole paper

// A decision on a result wins over a decision on its whole paper.
export const effectiveDecision = (decisions, index) => decisions?.[index] || decisions?.[PAPER] || null;

// A result's estimand and model as the manager read them (the paper's context).
const ESTIMAND_COLUMNS = [
  'design', 'estimand_population', 'estimand_treatments', 'estimand_variable', 'intercurrent_events', 'summary_measure',
  'model', 'missing_data', 'numbers_are', 'vs_plan', 'differences',
];

export const COLUMNS = [
  'paper_id', 'source', 'id', 'doi', 'title', 'year', 'text_used', 'extracted_by', 'result', 'outcome_measure',
  'timepoint', 'analysis_population', 'adjusted', 'relevance', 'confidence', ...ESTIMAND_COLUMNS, 'group', 'statistic',
  'value', 'ci_level', 'describes', 'quote', 'location', 'quote_check', 'number_in_quote', 'concerns', 'flags', 'decision',
  'decision_reason',
];

// The estimand columns of a paper's result; empty when the manager has not read them.
function estimandColumns(paper, index) {
  const context = paper.context?.status === 'done' ? paper.context.result : null;
  const read = context?.results.find((c) => c.result === index + 1);
  if (!read) return Object.fromEntries(ESTIMAND_COLUMNS.map((c) => [c, '']));
  return {
    design: context.design, estimand_population: read.population, estimand_treatments: read.treatments,
    estimand_variable: read.variable, intercurrent_events: read.intercurrent_events, summary_measure: read.summary_measure,
    model: read.model, missing_data: read.missing_data, numbers_are: read.numbers_are, vs_plan: read.vs_plan,
    differences: read.differences.join('; '),
  };
}

export function exportRows(papers, decisions, includedOnly) {
  const rows = [];
  for (const paper of papers.filter((p) => p.status === 'extracted' && p.extraction)) {
    const checks = paper.verification?.values || [];
    const flags = paper.verification?.flags || [];
    paper.extraction.results.forEach((result, index) => {
      const decision = effectiveDecision(decisions[paper.id], index);
      if (includedOnly && decision?.decision !== 'include') return;
      const estimand = estimandColumns(paper, index);
      result.values.forEach((value, j) => {
        const check = checks[index]?.[j] || {};
        rows.push({
          paper_id: paper.id, source: paper.source, id: paper.ext_id, doi: paper.doi || '', title: paper.title,
          year: paper.year || '', text_used: paper.fulltext_kind || '', extracted_by: paper.extracted_by || '',
          result: index + 1, outcome_measure: result.outcome_measure, timepoint: result.timepoint,
          analysis_population: result.analysis_population, adjusted: result.adjusted, relevance: result.relevance,
          confidence: result.confidence, ...estimand, group: value.group, statistic: value.statistic, value: value.value,
          ci_level: value.ci_level ?? '',
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

// Text a spreadsheet would take for a formula: it starts with =, +, -, @, a tab or a carriage return.
// Such a cell gets an apostrophe in front, so opening an export runs nothing that a paper's title or
// a quote put there. A number written as text (-2.2, +1e3) is still a number and is left alone.
const FORMULA = /^[=+\-@\t\r]/;
const NUMBER = /^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$/;

const cell = (v) => {
  let s = v === true ? 'True' : v === false ? 'False' : String(v ?? '');
  if (typeof v === 'string' && FORMULA.test(s) && !NUMBER.test(s)) s = `'${s}`;
  return /[",\r\n]/.test(s) ? `"${s.replaceAll('"', '""')}"` : s;
};

export const csvOf = (columns, rows) => [columns.join(','), ...rows.map((r) => columns.map((c) => cell(r[c])).join(','))].join('\r\n') + '\r\n';
export const toCsv = (rows) => csvOf(COLUMNS, rows);

// The agent activity log: one row per finished step or note, oldest first. `job` and `parent` say
// which steps were part of which (the manager's searches while it planned); `models` are the models its
// calls were served by; tokens count input with cache reads and writes.
export const LOG_COLUMNS = [
  'at', 'started', 'agent', 'models', 'step', 'phase', 'job', 'parent', 'paper_id', 'paper_title', 'text', 'detail',
  'ms', 'calls', 'input_tokens', 'output_tokens',
];

export function logRows(events, papers) {
  const titles = new Map(papers.map((p) => [p.id, p.title || p.ext_id]));
  return events.map((e) => ({
    at: e.at, started: e.started || '', agent: roleOf(e.agent), models: (e.models || []).join('; '), step: e.step, phase: e.phase,
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
