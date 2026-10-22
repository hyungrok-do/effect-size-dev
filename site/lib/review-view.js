// The review, one paper at a time, what the overlap check found, and the summary, drawn for both
// pages (site/app.js and web/app.js), which share the markup they fill. Every number in a summary
// comes from the papers through evidence.js: the model's text refers to values by key, e.g. [V3],
// and is shown here with their numbers. Styles are in review.css.

import { decisionsBasis } from './evidence.js';
import { STATISTICS } from './spec-panel.js';

const PAPER = -1; // result_index of a decision about the whole paper

// How each kind of overlap is named on a paper, before the others in its group, and in the list of groups.
const RELATIONS = {
  same_study: ['Same study as', 'one study reported more than once'],
  review_includes_study: ['A review and the studies it pools', 'a review and studies it pools'],
  reviews_share_studies: ['Reviews that share studies', 'reviews that share studies'],
  possible_overlap: ['Possibly the same participants as', 'possibly the same participants'],
};

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value == null || value === false) continue;
    if (key === 'class') el.className = value;
    else el.setAttribute(key, value === true ? '' : value);
  }
  for (const child of children.flat()) {
    if (child == null || child === false) continue;
    el.append(child instanceof Node ? child : String(child));
  }
  return el;
}

const $ = (selector) => document.querySelector(selector);
const badge = (text, kind = '') => h('span', { class: `badge ${kind}`.trim() }, text);
const short = (text, n) => (text.length > n ? `${text.slice(0, n - 1)}…` : text);
const plural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

// -- the review ---------------------------------------------------------------------

// How far a paper's review has got: every result decided (included, excluded or both), some, or
// none. `decisions` is the paper's {resultIndex: {decision}}.
export function reviewState(paper, decisions) {
  if (paper.status !== 'extracted') return 'open';
  const results = paper.extraction?.results || [];
  const chosen = results.map((_, i) => (decisions[i] || decisions[PAPER])?.decision).filter(Boolean);
  if (!results.length) return decisions[PAPER] ? (decisions[PAPER].decision === 'exclude' ? 'excluded' : 'included') : 'open';
  if (!chosen.length) return 'open';
  if (chosen.length < results.length) return 'partial';
  return chosen.every((d) => d === 'include') ? 'included' : chosen.every((d) => d === 'exclude') ? 'excluded' : 'mixed';
}
const REVIEWED = new Set(['included', 'excluded', 'mixed']);

// The navigator over the papers shown (#review-nav): a progress line, a chip per paper to jump to,
// and the pager. `current` is the id of the paper to show, `decisionsOf(paper)` its decisions,
// `show(id)` moves to another, and `counting` says whether to count the papers reviewed. Returns the
// paper to show, or null when there are none.
export function reviewNav({ papers, current, decisionsOf, show, counting }) {
  $('#review-nav').hidden = !papers.length;
  if (!papers.length) return null;
  const at = Math.max(0, papers.findIndex((p) => p.id === current));
  const states = papers.map((p) => reviewState(p, decisionsOf(p)));
  const reviewed = states.filter((st) => REVIEWED.has(st)).length;
  $('#review-progress-text').textContent = counting
    ? `Reviewed ${reviewed} of ${plural(papers.length, 'paper')}${reviewed === papers.length ? ' · all done; the summary is next' : ''}`
    : plural(papers.length, 'paper');
  $('#queue').replaceChildren(...papers.map((p, i) => h('li', {}, h('button', {
    type: 'button', 'data-state': states[i], 'aria-current': String(i === at), title: p.title || p.ext_id,
    'aria-label': `Paper ${i + 1}: ${p.title || p.ext_id} (${states[i] === 'open' ? 'not yet reviewed' : states[i]})`,
  }, String(i + 1)))));
  $('#queue').querySelectorAll('button').forEach((button, i) => { button.onclick = () => show(papers[i].id); });
  $('#review-position').textContent = `Paper ${at + 1} of ${papers.length}`;
  $('#review-prev').disabled = at === 0;
  $('#review-next').disabled = at === papers.length - 1;
  // Once every result of this paper is decided, Next is the thing to press.
  $('#review-next').classList.toggle('ready', REVIEWED.has(states[at]) && at < papers.length - 1);
  const nextOpen = papers.findIndex((p, i) => i !== at && !REVIEWED.has(states[i]) && p.status === 'extracted');
  $('#review-next-open').hidden = !counting || nextOpen < 0;
  $('#review-next-open').onclick = () => show(papers[nextOpen].id);
  $('#review-prev').onclick = () => show(papers[at - 1].id);
  $('#review-next').onclick = () => show(papers[at + 1].id);
  return papers[at];
}

// Arrow keys move through the review while it is on screen and nothing that takes typing has the focus.
export function reviewKeys(event) {
  if ($('#review').hidden || $('#review-nav').hidden || $('dialog[open]')) return;
  if (event.target.closest('input, select, textarea, [role="tab"]')) return;
  const button = { ArrowLeft: '#review-prev', ArrowRight: '#review-next' }[event.key];
  if (!button || $(button).disabled) return;
  event.preventDefault();
  $(button).click();
}

// -- overlap and the summary ----------------------------------------------------------

// What the overlap check found about one paper: each group it is in, the others in it, and which to use.
export function overlapNote(paper, report, papers) {
  const mine = `R${paper.id}`;
  const groups = (report?.groups || []).filter((g) => g.records.includes(mine));
  if (!groups.length) return null;
  const name = (key) => {
    const other = papers.find((p) => `R${p.id}` === key);
    return other ? `${key} “${short(other.title || other.ext_id, 60)}”` : key;
  };
  return h('div', { class: 'overlap-note' }, h('strong', {}, 'Overlap'), h('ul', {}, groups.map((g) => h('li', {},
    `${RELATIONS[g.relation]?.[0] || g.relation}: ${g.records.filter((r) => r !== mine).map(name).join('; ')}. ${g.evidence} `,
    g.use === mine ? 'Use this record’s numbers.' : g.use ? `Use the numbers of ${name(g.use)} instead.` : ''))));
}

// A record as the summary names it: key, first author and year.
function who(record, papers) {
  const p = papers.find((x) => `R${x.id}` === record);
  return p ? `${record} ${(p.authors || '').split(',')[0] || short(p.title || '', 40)} ${p.year || ''}`.trim() : record;
}

// What a value is, in words.
function describeValue(v, papers) {
  return `${STATISTICS[v.statistic] || v.statistic} of ${v.outcome}${v.timepoint ? ` at ${v.timepoint}` : ''}, ${v.group}`
    + `${v.n != null ? ` (n = ${v.n})` : ''}, ${who(v.record, papers)}`;
}

// Text from the model with [V3]-style keys shown as the values they name.
function withValues(text, values, papers) {
  return String(text || '').split(/(\[V\d+\])/).map((part) => {
    const v = /^\[V\d+\]$/.test(part) && values.get(part.slice(1, -1));
    return v ? h('span', { class: 'cand', title: `${v.key}: ${describeValue(v, papers)}` }, String(v.value), ' ', h('span', { class: 'cand-src' }, v.record)) : part;
  });
}

// The values for one number to find, one row per study arm (record, outcome, timepoint and group)
// and a column per statistic, in the order the summary lists them. A value whose quote was not found
// is marked in its cell and in the row's check.
function armTable(list, papers) {
  const rows = new Map();
  for (const v of list) {
    const id = JSON.stringify([v.record, v.outcome, v.timepoint, v.group]);
    if (!rows.has(id)) rows.set(id, { first: v, cells: new Map() });
    const cells = rows.get(id).cells;
    if (!cells.has(v.statistic)) cells.set(v.statistic, []);
    cells.get(v.statistic).push(v);
  }
  const statistics = Object.keys(STATISTICS).filter((st) => list.some((v) => v.statistic === st));
  return h('div', { class: 'table-wrap' }, h('table', { class: 'value-table' },
    h('thead', {}, h('tr', {}, ['Study', 'Outcome', 'Group', ...statistics.map((st) => STATISTICS[st]), 'Check'].map((t) => h('th', {}, t)))),
    h('tbody', {}, [...rows.values()].map(({ first, cells }) => {
      const paper = papers.find((x) => `R${x.id}` === first.record);
      const unchecked = statistics.filter((st) => (cells.get(st) || []).some((v) => !v.quote_found));
      return h('tr', {},
        h('td', { title: paper?.title || '' }, who(first.record, papers)),
        h('td', {}, first.outcome, first.timepoint ? h('span', { class: 'sub' }, first.timepoint) : null),
        h('td', {}, first.group),
        statistics.map((st) => h('td', { class: 'num' }, (cells.get(st) || []).flatMap((v, i) => [
          i ? ' / ' : '',
          h('span', { title: `${v.key}: ${v.describes}`, class: v.quote_found ? null : 'unchecked' }, String(v.value)),
        ]))),
        h('td', {}, unchecked.length
          ? badge(`quote not found: ${unchecked.map((st) => STATISTICS[st]).join(', ')}`, 'warn') : badge('quotes found', 'ok'),
        first.same_participants_as && [' ', badge(`same participants as ${first.same_participants_as}`, 'warn')]));
    }))));
}

// Every value, one row each, with its key.
function valueTable(list, papers) {
  return h('div', { class: 'table-wrap' }, h('table', { class: 'value-table' },
    h('thead', {}, h('tr', {}, ['Key', 'Study', 'Outcome', 'Group', 'Statistic', 'Value', 'n', 'Check'].map((t) => h('th', {}, t)))),
    h('tbody', {}, list.map((v) => {
      const paper = papers.find((x) => `R${x.id}` === v.record);
      return h('tr', {},
        h('td', {}, v.key),
        h('td', { title: paper?.title || '' }, who(v.record, papers)),
        h('td', {}, v.outcome, v.timepoint ? h('span', { class: 'sub' }, v.timepoint) : null),
        h('td', {}, v.group),
        h('td', {}, STATISTICS[v.statistic] || v.statistic),
        h('td', { class: 'num' }, String(v.value)),
        h('td', { class: 'num' }, v.n == null ? '' : String(v.n)),
        h('td', {}, badge(v.quote_found ? 'quote found' : 'quote not found', v.quote_found ? 'ok' : 'warn'),
          v.same_participants_as && [' ', badge(`same participants as ${v.same_participants_as}`, 'warn')]));
    }))));
}

// The summary: an overview, then for each number the plan needs the values that bear on it side by
// side with how they compare, then overlap, caveats and gaps. Nothing is combined or recommended.
// `decisions` is {paperId: {resultIndex: {decision}}}, to tell when the summary is out of date.
export function summaryView({ summary, overlap, papers, decisions }) {
  const { result, evidence } = summary;
  const values = new Map(evidence.values.map((v) => [v.key, v]));
  const stale = JSON.stringify(decisionsBasis(papers, decisions)) !== JSON.stringify(summary.basis);
  const text = (t) => withValues(t, values, papers);
  const groups = overlap?.groups || [];
  return [
    stale && h('p', { class: 'notice stale' }, 'Your decisions have changed since this was written. Write it again to use them.'),
    h('div', { class: 'summary-overview' }, text(result.overview)),
    ...result.needs.map((need) => {
      const list = need.values.map((k) => values.get(k)).filter(Boolean);
      return h('div', { class: 'summary-block need' }, h('h3', {}, need.need),
        h('p', {}, text(need.comparison)),
        list.length ? armTable(list, papers) : h('p', { class: 'muted' }, 'No reported value bears on this.'));
    }),
    (groups.length > 0 || result.overlap) && h('div', { class: 'summary-block' }, h('h3', {}, 'Overlap'),
      result.overlap && h('p', {}, text(result.overlap)),
      groups.length > 0 && h('ul', {}, groups.map((g) => h('li', {},
        `${g.records.join(', ')}: ${RELATIONS[g.relation]?.[1] || g.relation}. ${g.evidence}${g.use ? ` Use ${g.use}.` : ''}`)))),
    result.caveats.length > 0 && h('div', { class: 'summary-block' }, h('h3', {}, 'Check before using them'),
      h('ul', {}, result.caveats.map((x) => h('li', {}, text(x))))),
    result.gaps.length > 0 && h('div', { class: 'summary-block' }, h('h3', {}, 'What the sources don’t give'),
      h('ul', {}, result.gaps.map((x) => h('li', {}, text(x))))),
    h('details', { class: 'summary-block' }, h('summary', {}, `Every value kept (${plural(evidence.values.length, 'value')} from ${plural(evidence.records.length, 'record')})`),
      valueTable(evidence.values, papers)),
  ].filter(Boolean);
}
