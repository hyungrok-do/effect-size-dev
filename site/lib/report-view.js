// Shared table-only presentation for the browser and local editions.
import { csvOf, download } from './export.js';
import { factValue, STATUSES } from './reporting.js';
const h = (tag, attrs = {}, ...children) => {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k === 'className' ? 'class' : k, v);
  }
  for (const c of children.flat(Infinity)) if (c != null && c !== false) el.append(c instanceof Node ? c : String(c));
  return el;
};
const entries = (items) => h('div', { class: 'table-entries' }, items.map((x) => h('div', {}, x)));
const missing = (x) => x || 'Not reported';
const cellText = (value) => value instanceof Node ? (value.classList?.contains('table-entries') ? [...value.children].map((c) => c.textContent).join('\n') : value.textContent) : value;
function grid(id, title, headers, rows, plainRows = rows) {
  const columns = headers.map((_, i) => `c${i}`);
  const csvRows = plainRows.map((r) => Object.fromEntries(columns.map((c, i) => [c, cellText(r[i])])));
  // Use readable header names, while preserving the CSV export's formula protection.
  const exportRows = csvRows.map((r) => Object.fromEntries(headers.map((label, i) => [label, r[columns[i]]])));
  return h('section', { class: 'evidence-table-section', 'data-table': id },
    h('div', { class: 'evidence-table-heading' }, h('h3', {}, title),
      h('button', { type: 'button', class: 'small secondary', onclick: () => download(`${id}.csv`, csvOf(headers, exportRows), 'text/csv;charset=utf-8') }, 'Export table CSV')),
    h('div', { class: 'evidence-table-scroll', tabindex: '0', role: 'region', 'aria-label': title },
      h('table', { class: 'evidence-table' }, h('caption', {}, title),
        h('thead', {}, h('tr', {}, headers.map((label) => h('th', { scope: 'col' }, label)))),
        h('tbody', {}, rows.length ? rows.map((row) => h('tr', {}, row.map((cell) => h('td', {}, cell))))
          : h('tr', {}, h('td', { colspan: headers.length }, 'No retained analyses.'))))));
}
const numberLabel = (v) => `${v.group}: ${v.statistic} = ${v.value}${v.ci_level != null ? ` (${v.ci_level}% CI)` : ''}`;
const definition = (a, section) => entries(Object.entries(a.profile?.[section] || {}).map(([key, fact]) =>
  [key.replaceAll('_', ' '), ': ', missing(fact.value), ` [${fact.status}]`].join('')));
export function evidenceTablesView(tables, evidence, { stale = false, sourceLink = null } = {}) {
  const values = new Map((evidence.values || []).map((v) => [v.key, v]));
  const analyses = new Map(tables.analyses.map((a) => [a.id, a]));
  const estimateList = (ids) => entries(ids.flatMap((id) => {
    const a = analyses.get(id);
    const vs = (a?.values || []).map((k) => values.get(k)).filter(Boolean);
    return [h('strong', {}, id), ...(vs.length ? vs.map((v) => h('a', { href: `#evidence-value-${v.key}` }, numberLabel(v))) : ['No numerical result reported'])];
  }));
  const groups = tables.groups;
  const findings = grid('population-findings', 'Population-specific findings',
    ['Population', 'Comparison', 'Outcome / time', 'Target parameter', 'Reports / analyses', 'Reported effects and uncertainty', 'Combination status', 'Combined effect / uncertainty'],
    (tables.findings || []).map((g) => [entries([missing(g.population), `Eligibility: ${g.eligibility}`, `Setting: ${g.setting}`, `Analysis set: ${g.analysis_set}`, `Subgroup: ${g.subgroup}`]), missing(g.comparison), `${missing(g.outcome)} / ${missing(g.timepoint)}`, entries([missing(g.parameter), g.conditioning, `Scale: ${g.scale}`, `Units: ${g.units}`, `Direction: ${g.direction}`]),
      entries([g.id, g.records.join(', '), g.analyses.join(', ')]), estimateList(g.analyses),
      entries(g.groups.map((id) => `${id}: ${STATUSES[groups.find((x) => x.id === id).status]}`)), 'Not calculated']));
  const analysisTable = grid('analysis-extraction', 'Population, formulation and target parameters',
    ['Analysis / source', 'Population', 'Statistical formulation', 'Target parameters', 'Reported results', 'Verification', 'Unresolved information'],
    tables.analyses.map((a) => [entries([a.id, sourceLink ? sourceLink(a.record) : a.title, a.source_type || 'Source type unknown', `Decision: ${a.decision || 'undecided'}`]),
      a.profile ? definition(a, 'population') : a.population,
      a.profile ? definition(a, 'formulation') : 'Structured formulation not yet extracted',
      a.profile ? definition(a, 'target') : 'Structured target parameter not yet extracted',
      estimateList([a.id]), a.verification.replaceAll('_', ' ').toUpperCase(), entries(a.flags.length ? a.flags : ['None flagged'])]));
  const compatibility = grid('mathematical-feasibility', 'Mathematical basis for combining studies',
    ['Group / analyses', 'Population / target', 'Decision', 'Differences / reason', 'Mapping', 'Required inputs / availability', 'Assumptions / references', 'Dependence / unresolved checks'],
    groups.map((g) => [entries([g.id, g.analyses.join(', ')]), entries([g.population, g.parameter]), STATUSES[g.status],
      entries([g.reason, ...(g.differences || [])]), g.mapping || 'Not established',
      entries([`Available: ${g.inputs_available}`, ...(g.required_inputs || [])]),
      entries([...(g.assumptions || []), ...(g.methodological_sources || [])]),
      entries([g.dependence, ...(g.validation_notes || [])])]));
  const sourceRows = tables.analyses.flatMap((a) => (a.values || []).map((k) => values.get(k)).filter(Boolean).map((v) => [
    h('span', { id: `evidence-value-${v.key}` }, `${a.id} / ${v.key}`), v.group, v.statistic, v.value,
    v.ci_level == null ? 'Not stated / not a CI' : `${v.ci_level}%`, v.describes,
    entries([v.location, v.quote]), entries([v.quote_found ? 'Source match; see analysis audit' : 'Needs attention', v.same_participants_as ? `Same participants as ${v.same_participants_as}` : '']),
  ]));
  const numberTable = grid('reported-values', 'Every reported value and its source',
    ['Analysis / value', 'Group', 'Statistic', 'Value', 'CI level', 'Meaning', 'Source passage', 'Source check'], sourceRows);
  const fieldTable = grid('field-provenance', 'Evidence for each extracted field',
    ['Analysis', 'Field', 'Value', 'Evidence status', 'Source passage', 'Derivation / ambiguity', 'Quote check'],
    tables.analyses.flatMap((a) => (a.facts || []).map((f) => [a.id, f.field, missing(f.value), f.status,
      entries([f.location, f.quote]), f.rationale || '—', f.quote_check || 'Not checked'])));
  return [
    stale && h('p', { class: 'notice stale' }, 'Evidence, decisions or the target specification changed. Rebuild the tables before using them.'),
    ...tables.warnings.map((w) => h('p', { class: 'notice' }, w)), findings, analysisTable, compatibility, numberTable,
    h('details', { class: 'evidence-field-details' }, h('summary', {}, 'Field-level source evidence'), fieldTable),
    grid('combination-statuses', 'Combination statuses', ['Status', 'Meaning', 'Calculation'], [
      ['DIRECT SYNTHESIS', 'Compatible definitions and inputs; synthesis model still required', 'Not calculated'],
      ['TRANSFORM THEN SYNTHESIZE', 'Proposed mapping and required inputs available', 'Not calculated'],
      ['ASSUMPTION-DEPENDENT', 'Additional assumptions or sensitivity analysis required', 'Not calculated'],
      ['INSUFFICIENT INFORMATION', 'Missing inputs or unresolved extraction/compatibility checks', 'Unavailable'],
      ['KEEP SEPARATE', 'Different targets without a justified common mapping', 'Separate estimates'],
    ]),
  ].filter(Boolean);
}
