// The review, one paper at a time, what the overlap check found, and the conclusion, drawn for both
// pages (site/app.js and web/app.js), which share the markup they fill. Every number in a conclusion
// comes from the evidence code computed (evidence.js): the model's text refers to candidates by key,
// e.g. [S2], and is shown here with their values. Styles are in review.css.

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
export const fmt = (x) => (x == null ? '' : Number.isInteger(x) ? String(x) : String(Number(x.toPrecision(4))));

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
    ? `Reviewed ${reviewed} of ${plural(papers.length, 'paper')}${reviewed === papers.length ? ' · all done; the conclusion is next' : ''}`
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

// -- overlap and the conclusion -------------------------------------------------------

// A candidate's number and what it is.
export function describeCandidate(key, evidence, papers) {
  const who = (record) => {
    const p = papers.find((x) => `R${x.id}` === record);
    return p ? `${record} ${(p.authors || '').split(',')[0] || short(p.title || '', 40)} ${p.year || ''}`.trim() : record;
  };
  const where = (c) => `${c.outcome}${c.timepoint ? ` at ${c.timepoint}` : ''}`;
  const summary = evidence.summaries.find((c) => c.key === key);
  if (summary) {
    const what = STATISTICS[summary.statistic] || summary.statistic;
    const studies = plural(summary.records.length, 'study', 'studies');
    return summary.method === 'pooled_sd'
      ? { value: fmt(summary.value), label: `Pooled ${what} of ${where(summary)}, ${summary.count} arms in ${studies} (n = ${fmt(summary.n)})` }
      : { value: fmt(summary.value), label: `Median ${what} of ${where(summary)}, range ${fmt(summary.min)}–${fmt(summary.max)}, ${summary.count} values in ${studies}` };
  }
  const v = evidence.values.find((c) => c.key === key);
  if (!v) return null;
  return {
    value: fmt(v.value),
    label: `${STATISTICS[v.statistic] || v.statistic} of ${where(v)}, ${v.group}${v.n != null ? ` (n = ${fmt(v.n)})` : ''}, `
      + `${who(v.record)}${v.quote_found ? '' : '; quote not found'}${v.dropped_for_overlap ? '; left out for overlap' : ''}`,
  };
}

// Text from the model with [S2]-style keys shown as the candidates' numbers.
function withValues(text, evidence, papers) {
  return String(text || '').split(/(\[(?:S|V)\d+\])/).map((part) => {
    const match = part.match(/^\[((?:S|V)\d+)\]$/);
    const c = match && describeCandidate(match[1], evidence, papers);
    return c ? h('span', { class: 'cand', title: `${match[1]}: ${c.label}` }, c.value) : part;
  });
}

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
    g.use === mine ? 'Use this record.' : g.use ? `Use ${name(g.use)}; this record is left out of the conclusion.` : ''))));
}

// The conclusion: the summary, a recommended value for each number the plan needs (with the
// alternatives), the overlap found, caveats, gaps, and every candidate. `decisions` is
// {paperId: {resultIndex: {decision}}}, to tell when the conclusion is out of date.
export function conclusionView({ conclusion, overlap, papers, decisions }) {
  const { result, evidence } = conclusion;
  const stale = JSON.stringify(decisionsBasis(papers, decisions)) !== JSON.stringify(conclusion.basis);
  const text = (t) => withValues(t, evidence, papers);
  const candidate = (key) => {
    const d = key && describeCandidate(key, evidence, papers);
    return d ? [h('span', { class: 'value' }, d.value), h('span', { class: 'label' }, d.label)] : [h('span', { class: 'muted' }, 'Nothing fits')];
  };
  const groups = overlap?.groups || [];
  return [
    stale && h('p', { class: 'notice stale' }, 'Your decisions have changed since this was written. Write it again to use them.'),
    h('div', { class: 'conclusion-summary' }, text(result.summary)),
    h('div', { class: 'conclusion-block' }, h('h3', {}, 'Recommended values'),
      h('div', { class: 'table-wrap' }, h('table', { class: 'answers' },
        h('thead', {}, h('tr', {}, ['Needed', 'Recommended', 'Confidence', 'Why'].map((t) => h('th', {}, t)))),
        h('tbody', {}, result.answers.map((a) => h('tr', {},
          h('td', {}, a.need),
          h('td', {}, candidate(a.candidate),
            a.alternatives.length > 0 && h('span', { class: 'alt' }, 'Also: ',
              a.alternatives.flatMap((k, i) => [i ? '; ' : '', ...text(`[${k}]`), ' ', describeCandidate(k, evidence, papers)?.label || '']))),
          h('td', {}, badge(a.confidence, { high: 'ok', medium: 'warn', low: 'err' }[a.confidence])),
          h('td', {}, text(a.reasoning)))))))),
    (groups.length > 0 || result.overlap_handling) && h('div', { class: 'conclusion-block' }, h('h3', {}, 'Overlap'),
      result.overlap_handling && h('p', {}, text(result.overlap_handling)),
      groups.length > 0 && h('ul', {}, groups.map((g) => h('li', {},
        `${g.records.join(', ')}: ${RELATIONS[g.relation]?.[1] || g.relation}. ${g.evidence}${g.use ? ` Use ${g.use}.` : ''}`)))),
    result.caveats.length > 0 && h('div', { class: 'conclusion-block' }, h('h3', {}, 'Check before relying on it'),
      h('ul', {}, result.caveats.map((x) => h('li', {}, text(x))))),
    result.gaps.length > 0 && h('div', { class: 'conclusion-block' }, h('h3', {}, 'What the sources don’t give'),
      h('ul', {}, result.gaps.map((x) => h('li', {}, text(x))))),
    h('details', { class: 'conclusion-block' }, h('summary', {}, `All candidates (${evidence.summaries.length} summaries, ${evidence.values.length} values)`),
      h('div', { class: 'table-wrap' }, h('table', {},
        h('thead', {}, h('tr', {}, ['Key', 'Value', 'What'].map((t) => h('th', {}, t)))),
        h('tbody', {}, [...evidence.summaries, ...evidence.values].map((x) => {
          const d = describeCandidate(x.key, evidence, papers);
          return h('tr', {}, h('td', {}, x.key), h('td', { class: 'num' }, d.value), h('td', {}, d.label));
        }))))),
  ].filter(Boolean);
}
