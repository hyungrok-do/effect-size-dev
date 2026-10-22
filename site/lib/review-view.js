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
    body.replaceChildren(h('div', { class: 'table-wrap' }, h('table', { class: 'authors-table' },
      h('thead', {}, h('tr', {}, ['#', 'Name', ...(roles ? ['Role'] : []), 'Affiliation'].map((t) => h('th', { scope: 'col' }, t)))),
      h('tbody', {}, list.map((a, i) => h('tr', {},
        h('td', { class: 'num' }, String(i + 1)), h('td', {}, a.name),
        roles && h('td', {}, a.role || ''), h('td', {}, a.affiliation || '—')))))));
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

// One group's numbers in the usual shorthand, as printed: mean ± SD, median [Q1–Q3], events/n, an
// effect with its 95% CI, p. Returns {text, n}; anything else is listed by name.
export function groupLine(values) {
  const left = [...values];
  const has = (stat) => left.some((v) => v.statistic === stat);
  const take = (stat) => {
    const i = left.findIndex((v) => v.statistic === stat);
    return i < 0 ? null : left.splice(i, 1)[0].value;
  };
  const ci = () => (has('ci_lower') && has('ci_upper') ? ` (95% CI ${take('ci_lower')} to ${take('ci_upper')})` : '');
  const n = take('n');
  const parts = [];
  if (has('mean')) {
    const mean = take('mean');
    parts.push(has('sd') ? `${mean} ± ${take('sd')}` : has('se') ? `${mean} ± ${take('se')} (SE)` : `mean ${mean}${ci()}`);
  }
  if (has('mean_change')) {
    const change = take('mean_change');
    parts.push(has('sd_change') ? `change ${change} ± ${take('sd_change')}` : `change ${change}${ci()}`);
  }
  if (has('median')) {
    const median = take('median');
    parts.push(has('q1') && has('q3') ? `median ${median} [${take('q1')}–${take('q3')}]`
      : has('min') && has('max') ? `median ${median} (${take('min')}–${take('max')})` : `median ${median}`);
  }
  if (has('events')) {
    const events = take('events');
    parts.push(`${events}${n != null ? `/${n}` : ''} events${has('percent') ? ` (${take('percent')}%)` : ''}`);
  }
  if (has('percent')) parts.push(`${take('percent')}%`);
  for (const [stat, label] of Object.entries(EFFECTS)) while (has(stat)) parts.push(`${label} ${take(stat)}${ci()}`);
  if (has('ci_lower') || has('ci_upper')) parts.push(`95% CI ${take('ci_lower') ?? '?'} to ${take('ci_upper') ?? '?'}`);
  if (has('p_value')) parts.push(`p = ${take('p_value')}`);
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
const toCheck = (checks) => checks.filter((c) => c && (!c.number_in_quote || c.quote !== 'exact')).length;

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
  if (check.quote === 'exact') return badge('quote found', 'ok');
  if (check.quote === 'approximate') return badge('near match', 'warn');
  return check.number_in_source ? badge('number in text, quote not matched', 'warn') : badge('not found', 'err');
}

// A result for review: what was measured and when, Include and Exclude, a line per group (mean ± SD,
// n), and a warning only when there is one; the quotes and their checks are folded away.
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
    resultLines(result),
    notes.length > 0 && h('p', { class: 'flags' }, notes.join(' · ')),
    h('details', { class: 'quotes' }, h('summary', {}, 'Quotes'),
      h('p', { class: 'reason' }, [POPULATIONS[result.analysis_population] || result.analysis_population,
        result.adjusted !== 'unclear' && result.adjusted, `${result.confidence} confidence`].filter(Boolean).join(' · ')),
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

// The values for one number to find, one row per study arm (record, outcome, timepoint and group)
// and a column per statistic, in the order the summary lists them. A value whose quote was not found
// is marked in its cell and in the row's check.
function armTable(list, study) {
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
      const unchecked = statistics.filter((st) => (cells.get(st) || []).some((v) => !v.quote_found));
      return h('tr', {},
        h('td', {}, study.link(first.record)),
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
function valueTable(list, study) {
  return h('div', { class: 'table-wrap' }, h('table', { class: 'value-table' },
    h('thead', {}, h('tr', {}, ['Key', 'Study', 'Outcome', 'Group', 'Statistic', 'Value', 'n', 'Check'].map((t) => h('th', {}, t)))),
    h('tbody', {}, list.map((v) => h('tr', {},
      h('td', {}, v.key),
      h('td', {}, study.link(v.record)),
      h('td', {}, v.outcome, v.timepoint ? h('span', { class: 'sub' }, v.timepoint) : null),
      h('td', {}, v.group),
      h('td', {}, STATISTICS[v.statistic] || v.statistic),
      h('td', { class: 'num' }, String(v.value)),
      h('td', { class: 'num' }, v.n == null ? '' : String(v.n)),
      h('td', {}, badge(v.quote_found ? 'quote found' : 'quote not found', v.quote_found ? 'ok' : 'warn'),
        v.same_participants_as && [' ', badge(`same participants as ${v.same_participants_as}`, 'warn')]))))));
}

// The summary: an overview, then for each number the plan needs the values that bear on it side by
// side with how they compare, then overlap, caveats and gaps. Nothing is combined or recommended.
// Each study is linked to its page through `linksOf(paper)`, {label: url}. `decisions` is
// {paperId: {resultIndex: {decision}}}, to tell when the summary is out of date.
export function summaryView({ summary, overlap, papers, decisions, linksOf }) {
  const { result, evidence } = summary;
  const values = new Map(evidence.values.map((v) => [v.key, v]));
  const study = studies(papers, linksOf);
  const stale = JSON.stringify(decisionsBasis(papers, decisions)) !== JSON.stringify(summary.basis);
  const text = (t) => withValues(t, values, study);
  const groups = overlap?.groups || [];
  return [
    stale && h('p', { class: 'notice stale' }, 'Your decisions have changed since this was written. Write it again to use them.'),
    h('div', { class: 'summary-overview' }, text(result.overview)),
    ...result.needs.map((need) => {
      const list = need.values.map((k) => values.get(k)).filter(Boolean);
      return h('div', { class: 'summary-block need' }, h('h3', {}, need.need),
        h('p', {}, text(need.comparison)),
        list.length ? armTable(list, study) : h('p', { class: 'muted' }, 'No reported value bears on this.'));
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
    h('details', { class: 'summary-block' }, h('summary', {}, `Every value kept (${plural(evidence.values.length, 'value')} from ${plural(evidence.records.length, 'record')})`),
      valueTable(evidence.values, study)),
  ].filter(Boolean);
}
