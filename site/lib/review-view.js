// The review, one paper at a time, what the overlap check found, and the summary, drawn for both
// pages (site/app.js and web/app.js), which share the markup they fill. Each result shows what it
// estimates and with which model, as the manager read them, before its numbers. Every number in a
// summary comes from the papers through evidence.js: the model's text refers to values by key, e.g.
// [V3], and is shown here with their numbers. Styles are in review.css.

import { cleanAffiliations } from './affiliations.js';
import { decisionsBasis } from './evidence.js';
import { STATISTICS } from './spec-panel.js';
import { PLANNING_VERSION } from './planning.js';

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
    else if (key.startsWith('on')) el.addEventListener(key.slice(2), value);
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

// Which folded sections the reader has opened, by key, so that redrawing a card or the summary keeps
// them open.
const OPENED = new Set();
export function foldable(key, attrs, label, ...children) {
  return h('details', {
    ...attrs, open: OPENED.has(key) || null,
    ontoggle: (e) => { if (e.target.open) OPENED.add(key); else OPENED.delete(key); },
  }, h('summary', {}, label), ...children);
}

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

const popoverOpen = () => {
  try {
    return Boolean(document.querySelector('[popover]:popover-open'));
  } catch {
    return false; // a browser without popovers has none open
  }
};

// Arrow keys move through the review while it is on screen and nothing that takes typing has the focus.
export function reviewKeys(event) {
  if ($('#review').hidden || $('#review-nav').hidden || $('dialog[open]') || popoverOpen()) return;
  // Focus on nothing in particular or inside the review, and the navigator on screen: on the local
  // page every step is on one page, and the keys must not move the review from elsewhere.
  const target = event.target;
  if (!(target === document.body || target === document.documentElement || $('#review').contains(target))) return;
  if (target.closest('input, select, textarea, [role="tab"], .table-wrap')) return;
  const box = $('#review-nav').getBoundingClientRect();
  if (box.bottom < 0 || box.top > window.innerHeight) return;
  const button = { ArrowLeft: '#review-prev', ArrowRight: '#review-next' }[event.key];
  if (!button || $(button).disabled) return;
  event.preventDefault();
  $(button).click();
}

// -- overlap and the summary ----------------------------------------------------------

// What the overlap check found about one paper, a line per group it is in: the others in it, by key,
// and which record's numbers to use. Their titles and the evidence show on hover.
export function overlapNote(paper, report, papers) {
  const mine = `R${paper.id}`;
  const groups = (report?.groups || []).filter((g) => g.records.includes(mine));
  if (!groups.length) return null;
  const titleOf = (key) => {
    const other = papers.find((p) => `R${p.id}` === key);
    return other ? `${key}: ${other.title || other.ext_id}` : key;
  };
  return h('ul', { class: 'overlap-note' }, groups.map((g) => {
    const others = g.records.filter((r) => r !== mine);
    return h('li', { title: [g.evidence, ...others.map(titleOf)].join('\n') },
      `${RELATIONS[g.relation]?.[0] || g.relation}: ${others.join(', ')}. `,
      g.use === mine ? 'Use this record’s numbers.' : g.use ? `Use the numbers of ${g.use} instead.` : '');
  }));
}

// -- a paper's statistical context ---------------------------------------------------------

// How a result's estimand compares with the planned one, as the manager judged it: on the result, and
// in the summary, beside a recommendation's calculation and in its tables.
const FIT = {
  same: ['Same estimand as planned', 'Same as planned', 'ok'],
  close: ['Close to the planned estimand', 'Close to planned', 'warn'],
  different: ['Different estimand', 'Different', 'err'],
};
// The attributes of an estimand (ICH E9(R1)).
const ATTRIBUTES = [
  ['population', 'Population'], ['treatments', 'Treatments'], ['variable', 'Variable'],
  ['intercurrent_events', 'Intercurrent events'], ['summary_measure', 'Summary measure'],
];

// Whether a quote of the manager's was found in the paper (checkContext in verify.js); nothing without one.
function contextCheck(found) {
  if (found === 'exact') return badge('quote found', 'ok');
  if (found === 'approximate') return badge('near match', 'warn');
  return found === 'missing' ? badge('quote not found', 'err') : null;
}

// The manager's reading of one result ({read, check}: its ResultContext and its quote's check), or null.
function contextOf(paper, index) {
  if (paper.context?.status !== 'done') return null;
  const results = paper.context.result.results;
  const at = results.findIndex((c) => c.result === index + 1);
  return at < 0 ? null : { read: results[at], check: paper.context.checks?.results?.[at] ?? null };
}

// A paper's statistical context, under its header: the design as a statistician states it, the paper's
// own sample-size calculation, quoted with where it is and whether the quote was found, and anything
// else that bears on using its numbers. Until the manager has read it (paper.context), a line says it
// is reading it, or a button asks it to: `read()`, null while the page may not start that (a search
// is running). Nothing for a paper with no results.
export function studyContext(paper, { read = null } = {}) {
  if (paper.status !== 'extracted' || !paper.extraction?.results.length) return null;
  const context = paper.context;
  const ask = (label) => read && h('button', { type: 'button', class: 'secondary small', onclick: read }, label);
  if (context?.status === 'done') {
    const c = context.result;
    const row = (term, ...value) => h('div', {}, h('dt', {}, term), h('dd', {}, ...value));
    return h('dl', { class: 'study-context' },
      row('Design', c.design),
      c.sample_size && row('Sample-size calculation', h('q', {}, c.sample_size), ' ',
        c.sample_size_location && h('span', { class: 'where' }, c.sample_size_location), ' ', contextCheck(context.checks?.sample_size)),
      c.notes && row('Notes', c.notes));
  }
  if (context?.status === 'running') {
    return h('p', { class: 'study-context pending', role: 'status' }, 'The manager is reading the estimand and model…');
  }
  if (context?.status === 'error') {
    return h('div', { class: 'study-context pending' },
      h('p', {}, badge('Not read', 'warn'), ` Reading the estimand and model failed: ${context.error}`),
      ask('Read the estimand and model again'));
  }
  return h('div', { class: 'study-context pending' },
    h('p', {}, 'The manager has not yet read what these results estimate, or with which model.'),
    ask('Read the estimand and model'));
}

// What a result estimates and with which model, under its heading and before its numbers: how its
// estimand compares with the planned one and how it differs, its five attributes, the model and its
// handling of missing data, what the printed numbers are in terms of that model, and what they can do
// for the planned calculation.
function estimandBlock(read) {
  const [label, , kind] = FIT[read.vs_plan] || [read.vs_plan, '', ''];
  const row = (term, ...value) => h('div', {}, h('dt', {}, term), h('dd', {}, ...value));
  return h('div', { class: 'estimand', 'data-fit': read.vs_plan },
    h('div', { class: 'fit' }, badge(label, kind),
      read.differences.length > 0 && h('ul', { class: 'differences' }, read.differences.map((d) => h('li', {}, d)))),
    h('dl', {},
      row('Estimand', h('span', { class: 'attributes' }, ATTRIBUTES.map(([key, name]) => h('span', {},
        h('span', { class: 'k' }, name), h('span', {}, read[key] || '—'))))),
      row('Model', read.model,
        read.missing_data && h('span', { class: 'missing-data' }, h('span', { class: 'k' }, 'Missing data'), ' ', read.missing_data)),
      row('The numbers are', read.numbers_are),
      row('For the planned calculation', read.use)));
}

// -- a paper's citation ----------------------------------------------------------------

// The paper as cited, for the review: its title (linked to its free full text in PubMed Central
// when there is one, else to where it was found) with `action` beside it; its key, year, venue and
// authors, which open a table of every author and affiliation; then its identifiers, each linked,
// free full text first: PMCID, PMID, DOI, the Europe PMC ID of a record PubMed does not have, and
// trial registrations. `links` is the record's {label: url}; `warnings` are badges shown after the
// identifiers; `loadAuthors(paper)` fetches the authors of a record stored before they were kept.
export function paperHeader(paper, links, { warnings = [], action = null, loadAuthors = null } = {}) {
  const url = sourceUrl(paper, links);
  const title = paper.title || paper.ext_id;
  const venue = paper.source === 'clinicaltrials' ? 'ClinicalTrials.gov'
    : paper.journal || (paper.ext_id.startsWith('PPR') ? 'Preprint' : '');
  const id = (label, value, href, ...extra) => h('span', { class: 'id' }, label && `${label} `,
    href ? h('a', { href, target: '_blank', rel: 'noopener' }, value) : value, extra);
  const trials = [...new Set([...(paper.source === 'clinicaltrials' ? [paper.ext_id] : []), ...(paper.nct_ids || [])])];
  const ids = [
    paper.pmcid && id('PMCID', paper.pmcid, links.PMC, ' ', h('span', { class: 'free' }, 'free full text')),
    paper.pmid && id('PMID', paper.pmid, links.PubMed),
    paper.doi && id('DOI', paper.doi, links.DOI),
    paper.source === 'europepmc' && !paper.pmid && paper.ext_id !== paper.pmcid && id('Europe PMC', paper.ext_id, links['Europe PMC']),
    ...trials.map((nct) => id('', nct, links[nct] || `https://clinicaltrials.gov/study/${nct}`)),
  ].filter(Boolean);
  const shown = warnings.filter(Boolean);
  return [
    h('div', { class: 'paper-title' },
      h('h3', {}, url ? h('a', { href: url, target: '_blank', rel: 'noopener' }, title) : title),
      action),
    h('div', { class: 'meta cite' },
      badge(`R${paper.id}`, 'accent'),
      paper.year && h('span', {}, paper.year),
      venue && h('span', { class: 'venue' }, venue),
      authorsButton(paper, loadAuthors)),
    (ids.length > 0 || shown.length > 0) && h('div', { class: 'meta ids' }, ids, shown),
  ];
}

// The authors, as a button that opens a table of every author with their affiliations (for a registry
// record, the officials with their roles).
function authorsButton(paper, loadAuthors) {
  const popId = `authors-${paper.id}`;
  const body = h('div', { class: 'authors-body' });
  const heading = h('strong', { id: `${popId}-title` }, paper.source === 'clinicaltrials' ? 'Investigators' : 'Authors');
  const fill = (list) => {
    if (!list.length) {
      body.replaceChildren(h('p', { class: 'muted' }, paper.authors
        ? `${paper.authors} (affiliations are not available for this record).` : 'No authors are recorded for this record.'));
      return;
    }
    heading.textContent = `${paper.source === 'clinicaltrials' ? 'Investigators' : 'Authors'} (${list.length})`;
    const roles = list.some((a) => a.role);
    // Author lists saved before affiliations were cleaned hold one "; "-joined string.
    const affiliationsOf = (a) => a.affiliations || cleanAffiliations([a.affiliation]);
    const withAffiliations = list.filter((a) => affiliationsOf(a).length);
    const firstOnly = list.length > 1 && withAffiliations.length === 1 && withAffiliations[0] === list[0];
    body.replaceChildren(h('div', { class: 'table-wrap' }, h('table', { class: 'authors-table' },
      h('thead', {}, h('tr', {}, ['#', 'Name', ...(roles ? ['Role'] : []), 'Affiliation'].map((t) => h('th', { scope: 'col' }, t)))),
      h('tbody', {}, list.map((a, i) => h('tr', {},
        h('td', { class: 'num' }, String(i + 1)), h('td', {}, a.name),
        roles && h('td', {}, a.role || ''),
        h('td', {}, affiliationsOf(a).length ? affiliationsOf(a).map((x) => h('div', {}, x)) : '—'))))),
    firstOnly && h('p', { class: 'muted small' },
      'PubMed gives an affiliation for the first author only, as most journals reported them before 2014.')));
  };
  const open = async () => {
    let list = paper.author_list || [];
    if (!list.length && loadAuthors) {
      body.replaceChildren(h('p', { class: 'muted' }, 'Fetching the authors…'));
      try {
        list = await loadAuthors(paper);
      } catch {
        list = [];
      }
      paper.author_list = list;
    }
    fill(list);
  };
  const pop = h('div', {
    id: popId, popover: 'auto', class: 'authors-pop', role: 'dialog', 'aria-labelledby': `${popId}-title`,
    ontoggle: (e) => { if (e.newState === 'open') open(); },
  },
  h('div', { class: 'authors-head' }, heading,
    h('button', { type: 'button', class: 'secondary small', popovertarget: popId, popovertargetaction: 'hide' }, 'Close')),
  body);
  const label = paper.authors || (paper.source === 'clinicaltrials' ? 'Investigators' : 'Authors');
  return [
    h('button', { type: 'button', class: 'authors-btn', popovertarget: popId, 'aria-haspopup': 'dialog', title: 'Authors and affiliations' },
      label, h('span', { 'aria-hidden': 'true' }, ' ▾')),
    pop,
  ];
}

// -- a result in a line per group ----------------------------------------------------

const EFFECTS = {
  mean_difference: 'MD', standardized_mean_difference: 'SMD', odds_ratio: 'OR', risk_ratio: 'RR', hazard_ratio: 'HR',
  risk_difference: 'RD', icc: 'ICC', correlation: 'r',
};

// Statistics a CI can be reported for, and the CI bounds.
const ESTIMATES = new Set(['mean', 'mean_change', 'median', 'percent', ...Object.keys(EFFECTS)]);
const BOUNDS = new Set(['ci_lower', 'ci_upper']);

function intervalText(lower, upper) {
  const bounds = [lower, upper].filter(Boolean);
  const level = bounds[0]?.ci_level;
  const known = typeof level === 'number' && level > 0 && level < 100
    && bounds.every((v) => v.ci_level === level);
  return `${known ? `${level}% ` : ''}CI ${lower?.value ?? '?'} to ${upper?.value ?? '?'}`;
}

// One group's numbers in the usual shorthand, as printed: mean ± SD, median [Q1–Q3], events/n, an
// effect with its reported CI, p. A CI goes with the estimate it follows in the paper (else the one after
// it). Returns {text, n}; anything else is listed by name.
export function groupLine(values) {
  const ciOf = new Map(); // estimate -> {ci_lower, ci_upper}
  const loose = []; // bounds with no estimate to go with
  values.forEach((v, i) => {
    if (!BOUNDS.has(v.statistic)) return;
    const before = values.slice(0, i).reverse().find((x) => ESTIMATES.has(x.statistic));
    const owner = before || values.slice(i + 1).find((x) => ESTIMATES.has(x.statistic));
    const ci = owner && (ciOf.get(owner) || {});
    if (ci && ci[v.statistic] === undefined) {
      ci[v.statistic] = v;
      ciOf.set(owner, ci);
    } else {
      loose.push(v);
    }
  });
  const ci = (estimate) => {
    const c = ciOf.get(estimate);
    return c ? ` (${intervalText(c.ci_lower, c.ci_upper)})` : '';
  };
  const left = values.filter((v) => !BOUNDS.has(v.statistic));
  const has = (stat) => left.some((v) => v.statistic === stat);
  const take = (stat) => {
    const i = left.findIndex((v) => v.statistic === stat);
    return i < 0 ? null : left.splice(i, 1)[0];
  };
  const n = take('n')?.value ?? null;
  const parts = [];
  if (has('mean')) {
    const mean = take('mean');
    parts.push(has('sd') ? `${mean.value} ± ${take('sd').value}${ci(mean)}`
      : has('se') ? `${mean.value} ± ${take('se').value} (SE)${ci(mean)}` : `mean ${mean.value}${ci(mean)}`);
  }
  if (has('mean_change')) {
    const change = take('mean_change');
    parts.push(has('sd_change') ? `change ${change.value} ± ${take('sd_change').value}${ci(change)}` : `change ${change.value}${ci(change)}`);
  }
  if (has('median')) {
    const median = take('median');
    parts.push(has('q1') && has('q3') ? `median ${median.value} [${take('q1').value}–${take('q3').value}]${ci(median)}`
      : has('min') && has('max') ? `median ${median.value} (${take('min').value}–${take('max').value})${ci(median)}`
        : `median ${median.value}${ci(median)}`);
  }
  if (has('events')) {
    const events = take('events');
    const percent = has('percent') ? take('percent') : null;
    parts.push(`${events.value}${n != null ? `/${n}` : ''} events${percent ? ` (${percent.value}%)${ci(percent)}` : ''}`);
  }
  if (has('percent')) {
    const percent = take('percent');
    parts.push(`${percent.value}%${ci(percent)}`);
  }
  for (const [stat, label] of Object.entries(EFFECTS)) {
    while (has(stat)) {
      const effect = take(stat);
      parts.push(`${label} ${effect.value}${ci(effect)}`);
    }
  }
  for (let i = 0; i < loose.length; i++) {
    const lower = loose[i].statistic === 'ci_lower' ? loose[i] : null;
    const upper = lower == null ? loose[i] : loose[i + 1]?.statistic === 'ci_upper' ? loose[++i] : null;
    parts.push(intervalText(lower, upper));
  }
  if (has('p_value')) parts.push(`p = ${take('p_value').value}`);
  for (const v of left) parts.push(`${STATISTICS[v.statistic] || v.statistic} ${v.value}`);
  return { text: parts.join(' · '), n };
}

// A result as a line per group (arm, or contrast such as "A vs B"), in the order the paper gives them.
export function resultLines(result) {
  const groups = new Map();
  for (const v of result.values) {
    if (!groups.has(v.group)) groups.set(v.group, []);
    groups.get(v.group).push(v);
  }
  return h('table', { class: 'arms' }, h('tbody', {}, [...groups].map(([group, values]) => {
    const { text, n } = groupLine(values);
    return h('tr', {}, h('th', { scope: 'row' }, group), h('td', { class: 'est' }, text || '—'), h('td', { class: 'n' }, n != null ? `n = ${n}` : ''));
  })));
}

// How many of a result's values did not pass the quote check (`checks` from the verification).
const toCheck = (checks) => checks.filter((c) => c && (!c.number_in_quote || !c.number_in_source || c.quote !== 'exact')).length;

const POPULATIONS = {
  itt: 'ITT', modified_itt: 'Modified ITT', per_protocol: 'Per protocol', completers: 'Completers', unclear: 'Population unclear',
};
const EXCLUDE_REASONS = [
  'Different outcome', 'Different timepoint', 'Different population', 'Different comparison',
  'Same trial as another record', 'Numbers look wrong', 'Other',
];

function checkBadge(check) {
  if (!check) return '';
  if (!check.number_in_quote) return badge('number not in quote', 'err');
  if (!check.number_in_source) return badge('number not in source', 'err');
  if (check.quote === 'exact') return badge('quote found', 'ok');
  if (check.quote === 'approximate') return badge('near match', 'warn');
  return check.number_in_source ? badge('number in text, quote not matched', 'warn') : badge('not found', 'err');
}

// A result for review: what was measured and when, Include and Exclude, what it estimates and with
// which model (once the manager has read them), a line per group (mean ± SD, n), and a warning only
// when there is one; the quotes and their checks, the model's among them, are folded away.
// `decisions` is the paper's {resultIndex: {decision, reason}}; `decide(index, decision, reason)`
// records one (a null decision clears it).
export function resultBox(paper, result, index, decisions, decide) {
  const own = decisions[index];
  const decision = (own || decisions[PAPER])?.decision || '';
  const checks = paper.verification?.values?.[index] || [];
  const flags = (paper.verification?.flags || [])
    .filter((f) => f.startsWith(`result ${index + 1} `))
    .map((f) => f.replace(/^result \d+ \([^)]*\): /, ''));
  const notes = [...result.concerns, ...flags];
  const unchecked = toCheck(checks);
  const context = contextOf(paper, index);

  const reason = h('select', {
    'aria-label': 'Reason for excluding', hidden: decision !== 'exclude' || null,
    onchange: (e) => decide(index, 'exclude', e.target.value),
  }, h('option', { value: '' }, 'Reason…'), EXCLUDE_REASONS.map((r) => h('option', { value: r }, r)));
  reason.value = own?.reason || '';

  const head = h('div', { class: 'result-head' },
    h('div', {},
      h('h4', {}, result.outcome_measure, result.timepoint && h('span', { class: 'when' }, ` · ${result.timepoint}`)),
      (result.relevance !== 'direct' || result.confidence === 'low' || unchecked > 0) && h('div', { class: 'meta' },
        result.relevance !== 'direct' && badge(`${result.relevance} match`, result.relevance === 'partial' ? 'warn' : 'err'),
        result.confidence === 'low' && badge('low confidence', 'warn'),
        unchecked > 0 && badge(`${unchecked} ${unchecked === 1 ? 'quote' : 'quotes'} to check`, 'warn'))),
    h('div', { class: 'decide', role: 'group', 'aria-label': `Decision for ${result.outcome_measure}` },
      h('button', {
        type: 'button', class: 'secondary small include', 'aria-pressed': String(decision === 'include'),
        onclick: () => decide(index, own?.decision === 'include' ? null : 'include', ''),
      }, 'Include'),
      h('button', {
        type: 'button', class: 'secondary small exclude', 'aria-pressed': String(decision === 'exclude'),
        onclick: () => decide(index, own?.decision === 'exclude' ? null : 'exclude', own?.reason || ''),
      }, 'Exclude'),
      reason));

  const rows = result.values.map((v, j) => h('tr', {},
    h('td', {}, v.group),
    h('td', {}, STATISTICS[v.statistic] || v.statistic),
    h('td', { class: 'num' }, String(v.value)),
    h('td', { class: 'quote' }, h('q', {}, v.quote), h('span', {}, v.location)),
    h('td', {}, checkBadge(checks[j]))));

  return h('section', { class: 'result', 'data-decision': decision || null },
    head,
    context && estimandBlock(context.read),
    resultLines(result),
    notes.length > 0 && h('p', { class: 'flags' }, notes.join(' · ')),
    foldable(`quotes:${paper.id}:${index}`, { class: 'quotes' }, 'Quotes',
      h('p', { class: 'reason' }, [POPULATIONS[result.analysis_population] || result.analysis_population,
        result.adjusted !== 'unclear' && result.adjusted, `${result.confidence} confidence`].filter(Boolean).join(' · ')),
      context?.read.quote && h('p', { class: 'model-quote' }, h('span', { class: 'k' }, 'Model'), ' ', h('q', {}, context.read.quote), ' ',
        context.read.location && h('span', { class: 'where' }, context.read.location), ' ', contextCheck(context.check)),
      h('div', { class: 'table-wrap' }, h('table', {},
        h('thead', {}, h('tr', {}, ['Group', 'Statistic', 'Value', 'Quote', 'Check'].map((t) => h('th', { scope: 'col' }, t)))),
        h('tbody', {}, rows)))));
}

// Where each source's records are linked: a paper to its free full text in PubMed Central if it has
// one, else to the database it was found in; a registry record to its ClinicalTrials.gov page.
// `links` is the record's {label: url}.
const SOURCE_LINKS = { pubmed: 'PubMed', europepmc: 'Europe PMC' };
// A paper's free full text in PubMed Central comes first, when there is one.
export function sourceUrl(paper, links) {
  if (paper.source !== 'clinicaltrials' && links.PMC) return links.PMC;
  return links[paper.source === 'clinicaltrials' ? paper.ext_id : SOURCE_LINKS[paper.source]]
    || links.PubMed || links.PMC || links.DOI || Object.values(links)[0] || null;
}

// How the summary names and links records: by key, first author and year, linked to the record's
// page. `linksOf(paper)` gives a paper's links, {label: url}, as its card in the review shows them.
function studies(papers, linksOf) {
  const byKey = new Map(papers.map((p) => [`R${p.id}`, p]));
  const name = (record) => {
    const p = byKey.get(record);
    return p ? `${record} ${(p.authors || '').split(',')[0] || short(p.title || '', 40)} ${p.year || ''}`.trim() : record;
  };
  const url = (p) => sourceUrl(p, linksOf(p) || {});
  // A record as a link to its page, showing `text` (its name by default), with its title on hover.
  const link = (record, text = name(record)) => {
    const p = byKey.get(record);
    const href = p && url(p);
    return href ? h('a', { href, target: '_blank', rel: 'noopener', title: p.title || '' }, text) : h('span', { title: p?.title || '' }, text);
  };
  return { has: (record) => byKey.has(record), name, link };
}

// What a value is, in words.
function describeValue(v, study) {
  return `${STATISTICS[v.statistic] || v.statistic} of ${v.outcome}${v.timepoint ? ` at ${v.timepoint}` : ''}, ${v.group}`
    + `${v.n != null ? ` (n = ${v.n})` : ''}, ${study.name(v.record)}`;
}

// Text from the model with [V3]-style keys shown as the values they name, and records it names by
// key (R12) linked to their pages.
function withValues(text, values, study) {
  return String(text || '').split(/(\[V\d+\]|\bR\d+\b)/).map((part) => {
    if (study.has(part)) return study.link(part, part);
    const v = /^\[V\d+\]$/.test(part) && values.get(part.slice(1, -1));
    return v ? h('span', { class: 'cand', title: `${v.key}: ${describeValue(v, study)}` }, String(v.value), ' ', h('span', { class: 'cand-src' }, v.record)) : part;
  });
}

// A value's estimand (`estimands` maps E1, E2, ... to the evidence's estimands): how it compares with
// the planned one, with how it differs and the model on hover. A dash for a value from a result the
// manager has not read.
function estimandCell(v, estimands) {
  const e = estimands.get(v.estimand);
  if (!e) return h('td', { class: 'muted', title: 'Not read' }, '—');
  const [, label, kind] = FIT[e.vs_plan] || ['', e.vs_plan, ''];
  const why = e.differences?.length ? e.differences
    : [e.vs_plan === 'same' ? 'The same quantity as the planned estimand' : 'No difference listed'];
  return h('td', {}, h('span', { class: `badge fit ${kind}`.trim(), title: [...why, e.model && `Model: ${e.model}`].filter(Boolean).join('\n') }, label));
}

// What a recommendation's source result estimates, as the manager read it, beside its calculation: how
// its estimand compares with the planned one, how it differs, and the model behind its numbers; or that
// the manager has not read it. `readings` maps E1, E2, ... to the evidence's estimands, and is null for
// a summary written before the manager read them, which shows nothing here.
function sourceEstimand(effect, values, readings) {
  if (!readings) return null;
  const e = readings.get(values.get(effect.inputs[0])?.estimand);
  if (!e) {
    return h('div', { class: 'source-estimand', 'data-fit': 'unread' }, h('div', { class: 'fit' },
      badge('Estimand not read', 'warn'), h('span', { class: 'muted small' }, 'What the source result estimates, and with which model, is unknown.')));
  }
  const [label, , kind] = FIT[e.vs_plan] || [e.vs_plan, '', ''];
  return h('div', { class: 'source-estimand', 'data-fit': e.vs_plan },
    h('div', { class: 'fit' }, badge(label, kind),
      e.differences?.length > 0 && h('ul', { class: 'differences' }, e.differences.map((d) => h('li', {}, d)))),
    e.model && h('p', { class: 'model' }, h('span', { class: 'k' }, 'Model'), ' ', e.model));
}

// The values for one effect size to find, one row per study arm (record, outcome, timepoint and group)
// and a column per statistic, in the order the summary lists them, with the estimand of each when the
// manager has read them. A value whose quote was not found is marked in its cell and in the row's check.
function armTable(list, study, estimands) {
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
    h('thead', {}, h('tr', {}, ['Study', 'Outcome', 'Group', ...(estimands ? ['Estimand'] : []), ...statistics.map((st) => STATISTICS[st]), 'Check']
      .map((t) => h('th', {}, t)))),
    h('tbody', {}, [...rows.values()].map(({ first, cells }) => {
      const unchecked = statistics.filter((st) => (cells.get(st) || []).some((v) => !v.quote_found));
      return h('tr', {},
        h('td', {}, study.link(first.record)),
        h('td', {}, first.outcome, first.timepoint ? h('span', { class: 'sub' }, first.timepoint) : null),
        h('td', {}, first.group),
        estimands && estimandCell(first, estimands),
        statistics.map((st) => h('td', { class: 'num' }, (cells.get(st) || []).flatMap((v, i) => [
          i ? ' / ' : '',
          h('span', { title: `${v.key}: ${v.describes}`, class: v.quote_found ? null : 'unchecked' }, String(v.value)),
        ]))),
        h('td', {}, unchecked.length
          ? badge(`quote not found: ${unchecked.map((st) => STATISTICS[st]).join(', ')}`, 'warn') : badge('quotes found', 'ok'),
        first.same_participants_as && [' ', badge(`same participants as ${first.same_participants_as}`, 'warn')]));
    }))));
}

// Every value, one row each, with its key (and its estimand, when the manager has read them).
function valueTable(list, study, estimands) {
  return h('div', { class: 'table-wrap' }, h('table', { class: 'value-table' },
    h('thead', {}, h('tr', {}, ['Key', 'Study', 'Outcome', 'Group', ...(estimands ? ['Estimand'] : []), 'Statistic', 'Value', 'n', 'Check']
      .map((t) => h('th', {}, t)))),
    h('tbody', {}, list.map((v) => h('tr', {},
      h('td', {}, v.key),
      h('td', {}, study.link(v.record)),
      h('td', {}, v.outcome, v.timepoint ? h('span', { class: 'sub' }, v.timepoint) : null),
      h('td', {}, v.group),
      estimands && estimandCell(v, estimands),
      h('td', {}, STATISTICS[v.statistic] || v.statistic),
      h('td', { class: 'num' }, String(v.value)),
      h('td', { class: 'num' }, v.n == null ? '' : String(v.n)),
      h('td', {}, badge(v.quote_found ? 'quote found' : 'quote not found', v.quote_found ? 'ok' : 'warn'),
        v.same_participants_as && [' ', badge(`same participants as ${v.same_participants_as}`, 'warn')]))))));
}

// The head agent's recommendation for each outcome, with code-resolved arithmetic and, beside it, what
// its source result estimates as the manager read it; then, for each effect size the plan needs, what
// its values estimate and with which models, how they compare, and the values side by side; then
// overlap, caveats, gaps and the source evidence. A summary written before the estimands were read
// shows as it did.
// Each study is linked to its page through `linksOf(paper)`, {label: url}. `decisions` is
// {paperId: {resultIndex: {decision}}}, to tell when the summary is out of date.
export function summaryView({ summary, spec, overlap, papers, decisions, linksOf }) {
  const { result, evidence } = summary;
  const values = new Map(evidence.values.map((v) => [v.key, v]));
  // The manager's readings of the kept results' estimands (E1, E2, ...): none in a summary written before it
  // read them; the tables get a column for them once there is one.
  const readings = evidence.estimands ? new Map(evidence.estimands.map((e) => [e.key, e])) : null;
  const estimands = readings?.size ? readings : null;
  const study = studies(papers, linksOf);
  const stale = JSON.stringify(decisionsBasis(papers, decisions)) !== JSON.stringify(summary.basis)
    || (spec && summary.spec_basis && JSON.stringify(spec) !== JSON.stringify(summary.spec_basis));
  const text = (t) => withValues(t, values, study);
  const groups = overlap?.groups || [];
  return [
    stale && h('p', { class: 'notice stale' }, 'Your decisions have changed since this was written, or the study specification or evidence '
      + '(the manager’s reading of the papers among it) has changed. Write it again before using the recommendation.'),
    result.planning_version !== PLANNING_VERSION && h('p', { class: 'notice' },
      'This saved summary predates planning recommendations. Write it again to ask the head agent for a final effect size and justification.'),
    ...(result.recommendations || []).map((rec) => {
      const effect = rec.resolved;
      const label = { recommended: 'Recommended planning effect', provisional: 'Provisional planning effect',
        insufficient_evidence: 'Insufficient evidence for a numerical choice' }[rec.status] || rec.status;
      const number = (n) => String(Number(n.toPrecision(6)));
      const calc = (e) => h('div', { class: 'effect-calculation' },
        h('p', {}, e.contrast, ' · ', e.outcome, e.timepoint ? ` · ${e.timepoint}` : '', ' · ', e.scale),
        e.origin === 'planning_assumption' && h('p', {}, `Evidence estimate: ${number(e.basis_value)}. Planning target: ${number(e.value)}. The target is an explicit head-agent judgment, not a reported estimate.`),
        h('p', {}, 'Source: ', study.link(e.record), '. Inputs: ', ...text(e.inputs.map((k) => `[${k}]`).join(', '))),
        h('code', {}, e.formula));
      return h('section', { class: 'summary-block planning-recommendation', 'data-status': rec.status },
        h('h3', {}, rec.outcome), badge(label, rec.status === 'recommended' ? 'ok' : 'warn'),
        effect && h('p', { class: 'planning-effect' }, h('strong', {},
          `${effect.metric === 'cohen_d' ? 'Cohen d' : (STATISTICS[effect.metric] || effect.metric)} = ${number(effect.value)}`)),
        effect && calc(effect),
        effect && sourceEstimand(effect, values, readings),
        rec.estimand && h('p', {}, h('strong', {}, 'Target effect: '), text(rec.estimand)),
        h('p', {}, h('strong', {}, 'Justification: '), text(rec.justification)),
        rec.choice?.rationale && h('p', {}, text(rec.choice.rationale)),
        rec.importance_basis && h('p', {}, h('strong', {}, 'Clinical importance: '), text(rec.importance_basis)),
        rec.realism && h('p', {}, h('strong', {}, 'Realism: '), text(rec.realism)),
        rec.evidence_quality && h('p', {}, h('strong', {}, 'Evidence quality: '), text(rec.evidence_quality)),
        ...[['Assumptions', rec.assumptions], ['Other inputs for sample-size planning', rec.nuisance_parameters],
          ['Unresolved information', [...(rec.gaps || []), ...(rec.validation_notes || [])]]]
          .filter(([, items]) => items?.length).map(([title, items]) => h('div', {}, h('h4', {}, title),
            h('ul', {}, items.map((item) => h('li', {}, text(item)))))),
        rec.sensitivity?.length > 0 && h('div', {}, h('h4', {}, 'Sensitivity scenarios'),
          h('p', { class: 'muted small' }, 'Alternative planning assumptions; these are not confidence limits.'),
          ...rec.sensitivity.map((scenario) => h('div', { class: 'summary-block' },
            h('strong', {}, `${scenario.label}: ${number(scenario.resolved.value)}`), calc(scenario.resolved),
            h('p', {}, text(scenario.choice.rationale))))));
    }),
    h('div', { class: 'summary-overview' }, text(result.overview)),
    ...result.needs.map((need) => {
      const list = need.values.map((k) => values.get(k)).filter(Boolean);
      return h('div', { class: 'summary-block need' }, h('h3', {}, need.need),
        need.estimands ? [
          h('h4', { class: 'label' }, 'Estimands'), h('p', { class: 'estimands' }, text(need.estimands)),
          h('h4', { class: 'label' }, 'How they compare'), h('p', { class: 'comparison' }, text(need.comparison)),
        ] : h('p', {}, text(need.comparison)),
        list.length ? armTable(list, study, estimands) : h('p', { class: 'muted' }, 'No reported value bears on this.'));
    }),
    (groups.length > 0 || result.overlap) && h('div', { class: 'summary-block' }, h('h3', {}, 'Overlap'),
      result.overlap && h('p', {}, text(result.overlap)),
      groups.length > 0 && h('ul', {}, groups.map((g) => h('li', {},
        g.records.flatMap((r, i) => [i ? ', ' : '', study.link(r, r)]),
        `: ${RELATIONS[g.relation]?.[1] || g.relation}. ${g.evidence}`,
        g.use && [' Use ', study.link(g.use, g.use), '.'])))),
    result.caveats.length > 0 && h('div', { class: 'summary-block' }, h('h3', {}, 'Check before using them'),
      h('ul', {}, result.caveats.map((x) => h('li', {}, text(x))))),
    result.gaps.length > 0 && h('div', { class: 'summary-block' }, h('h3', {}, 'What the sources don’t give'),
      h('ul', {}, result.gaps.map((x) => h('li', {}, text(x))))),
    foldable('summary:every-value', { class: 'summary-block' },
      `Every value kept (${plural(evidence.values.length, 'value')} from ${plural(evidence.records.length, 'record')})`,
      valueTable(evidence.values, study, estimands)),
    foldable('summary:appraisal', { class: 'summary-block' }, 'Methodological evidence used by the head agent',
      ...evidence.records.map((record) => h('div', { class: 'summary-block' }, study.link(record.key),
        h('p', { class: 'muted small' }, record.fulltext_kind || 'Source type unknown'),
        record.appraisal?.length ? h('ul', {}, record.appraisal.map((a) => h('li', {},
          h('strong', {}, `${a.domain.replaceAll('_', ' ')}: `), a.assessment,
          h('blockquote', {}, a.quote), h('span', { class: 'muted small' }, a.location), ' ',
          badge(a.quote_found ? 'quote found' : 'quote not found', a.quote_found ? 'ok' : 'warn'))))
          : h('p', {}, 'No structured appraisal was extracted. Re-extract the report to collect supporting text.')))),
    h('p', { class: 'muted small' }, 'Methodological basis: ',
      h('a', { href: 'https://doi.org/10.1186/s13063-018-2884-0', target: '_blank', rel: 'noopener' }, 'DELTA²'), ', ',
      h('a', { href: 'https://www.cochrane.org/authors/handbooks-and-manuals/handbook/current/chapter-05', target: '_blank', rel: 'noopener' }, 'Cochrane Handbook'), ', ',
      h('a', { href: 'https://database.ich.org/sites/default/files/E9-R1_Step4_Guideline_2019_1203.pdf', target: '_blank', rel: 'noopener' }, 'ICH E9(R1)'),
      '. Recommendations apply selected principles; they do not constitute a complete systematic review or formal risk-of-bias assessment.'),
  ].filter(Boolean);
}
