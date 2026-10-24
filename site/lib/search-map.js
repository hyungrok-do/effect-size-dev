// The search map: how the search works, drawn as the stages it runs through, each with the agents
// that do it, a sentence on its logic, and how far it has got. Both pages draw it from the search
// and its records as stored, so it fills in as the search runs. Styles are in search-map.css.

import { LEVEL_NAMES, LEVELS } from './generated.js';
import { roleOf } from './providers.js';

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

const number = (n) => Number(n).toLocaleString('en-US');
const plural = (n, one, many = `${one}s`) => `${number(n)} ${n === 1 ? one : many}`;
const short = (text, n) => (text.length > n ? `${text.slice(0, n - 1)}…` : text);

// The roles' names when the page gives none (see providers.js for the roles).
const ROLE_NAMES = { manager: 'Manager', fetcher: 'Fetcher', worker: 'Workers', code: 'Code' };
const SOURCE_NAMES = { pubmed: 'PubMed', europepmc: 'Europe PMC', clinicaltrials: 'ClinicalTrials.gov' };

// The stages in the order the search runs them, and the progress stage that says each is under way.
// `how` is given the roles' names.
const STAGES = [
  { key: 'plan', title: 'Plan the search', agents: ['manager'], how: (n) => `${n.manager} builds terms for each concept and sets levels from broad to narrow.` },
  { key: 'search', title: 'Search from the top down', agents: ['code'], how: () => 'The levels run in order, reviews first; each record counts once.' },
  { key: 'screen', title: 'Screen titles and abstracts', agents: ['worker'], how: (n) => `${n.worker} includes, excludes or marks each record unsure (kept).` },
  { key: 'follow', title: 'Follow the reviews’ references', agents: ['code', 'worker'], how: () => 'Studies cited by the included reviews are added and screened.' },
  {
    key: 'extract', title: 'Read and extract', agents: ['code', 'fetcher', 'worker', 'manager'],
    how: (n) => `Text is fetched, ${n.worker} extracts each number with its quote, code checks it, ${n.manager} redoes what is flagged.`,
  },
  { key: 'overlap', title: 'Check for overlap', agents: ['manager'], how: (n) => `${n.manager} finds studies reported twice and reviews pooling studies in the set.` },
  { key: 'ready', title: 'Ready for review', agents: [], how: () => 'Extracted records wait for your review.' },
];
const STAGE_OF = {
  'planning the search': 'plan', 'running the search': 'search', screening: 'screen',
  "reading the reviews' references": 'follow', extracting: 'extract', 'checking for overlap': 'overlap', done: 'ready',
};
const ORDER = STAGES.map((s) => s.key);
const KEPT = new Set(['screened_in', 'unsure', 'fetching', 'extracting', 'extracted', 'no_fulltext', 'needs_manual', 'error']);

// Where each stage stands: 'done', 'active', 'stopped' (where a search that is not running stopped),
// or 'pending'. `running` says whether the search is running now.
const UNDER_WAY = new Set(['searching', 'screening', 'extracting']);

export function stageStates(project, running) {
  const states = Object.fromEntries(ORDER.map((k) => [k, 'pending']));
  // A run that finished: done, also after the specification is edited again (status spec_ready).
  // A run just started may still carry the last run's 'done' until it writes its own progress.
  const finished = project.progress?.stage === 'done';
  if (project.status === 'done' || (finished && !running && !UNDER_WAY.has(project.status))) {
    for (const k of ORDER) states[k] = 'done';
    return states;
  }
  const log = project.search_log || [];
  const current = (!finished && STAGE_OF[project.progress?.stage]) || (project.search_plan ? 'search' : 'plan');
  const at = ORDER.indexOf(current);
  ORDER.forEach((k, i) => { if (i < at) states[k] = 'done'; });
  // Records cited by the reviews are screened after the references are read.
  if (current === 'screen' && log.some((e) => e.level === 'cited')) states.follow = 'done';
  const started = project.search_plan || project.progress?.stage || ['searching', 'screening', 'extracting'].includes(project.status);
  if (started) states[current] = running ? 'active' : 'stopped';
  return states;
}

// The queries in the order the search runs them: the levels from the top down, then the registry.
function queriesOf(plan) {
  if (!plan) return [];
  const planned = plan.levels || (plan.pubmed_queries || []).map((query) => ({ level: 'primary', query }));
  return [
    ...planned.filter((l) => l.query.trim()).sort((a, b) => LEVELS.indexOf(a.level) - LEVELS.indexOf(b.level))
      .map((l) => ({ level: l.level, source: l.level === 'full_text' ? 'europepmc' : 'pubmed', query: l.query })),
    ...plan.clinicaltrials_queries.map((query) => ({ level: 'registry', source: 'clinicaltrials', query })),
  ];
}

// A bar split by count, with its legend.
function bar(parts) {
  const total = parts.reduce((a, p) => a + p.n, 0);
  if (!total) return null;
  return h('div', { class: 'map-bar-wrap' },
    h('div', { class: 'map-bar', role: 'img', 'aria-label': parts.map((p) => `${p.label} ${p.n}`).join(', ') },
      parts.filter((p) => p.n).map((p) => h('span', { 'data-kind': p.kind, style: `flex-grow: ${p.n}`, title: `${p.label}: ${p.n}` }))),
    h('ul', { class: 'map-legend' }, parts.filter((p) => p.n).map((p) => h('li', { 'data-kind': p.kind }, `${p.label} ${number(p.n)}`))));
}

const facts = (...items) => {
  const shown = items.filter(Boolean);
  return shown.length ? h('ul', { class: 'map-facts' }, shown.map((x) => h('li', {}, x))) : null;
};

// What each stage found, from the search and its records.
function details(key, { project, papers, state, progress, log, names }) {
  const plan = project.search_plan;
  if (key === 'plan') {
    if (!plan) return null;
    const levels = queriesOf(plan).filter((q) => q.level !== 'registry').length;
    return facts(`${plural(levels, 'level')} planned`, `${plural(plan.clinicaltrials_queries.length, 'ClinicalTrials.gov query', 'ClinicalTrials.gov queries')}`,
      plan.extra_ids.length > 0 && `${plural(plan.extra_ids.length, 'record')} named directly`);
  }
  if (key === 'search') {
    const queries = queriesOf(plan);
    if (!queries.length) return null;
    const entries = log.filter((e) => e.level !== 'cited' && e.level !== 'named');
    let step = 0;
    const rows = queries.map((q, i) => {
      const e = entries[i];
      const nodeState = e ? (e.error ? 'error' : 'done')
        : state === 'done' ? 'done'
          : state === 'active' && progress.done === i ? 'active' : state === 'stopped' && progress.done === i ? 'stopped' : 'pending';
      const result = e?.error ? `Failed: ${e.error}`
        : e?.hits != null ? [plural(e.hits, 'hit'), `top ${number(e.taken)} taken`, e.added != null && `${number(e.added)} new`].filter(Boolean).join(' → ')
          : nodeState === 'active' ? 'Searching…' : '';
      const mark = q.level === 'registry' ? '+' : String(++step);
      return h('li', { class: 'map-level', 'data-state': nodeState, 'data-level': q.level },
        h('span', { class: 'map-level-mark', 'aria-hidden': 'true' }, mark),
        h('div', { class: 'map-level-body' },
          h('div', { class: 'map-level-head' }, h('strong', {}, LEVEL_NAMES[q.level] || q.level),
            h('span', { class: 'map-source' }, SOURCE_NAMES[q.source])),
          h('code', { title: q.query }, short(q.query, 160)),
          result && h('span', { class: 'map-result' }, result)));
    });
    const collected = papers.filter((p) => !(p.found_in || []).length || (p.found_in || []).some((l) => l !== 'cited')).length;
    return [
      h('ol', { class: 'map-levels' }, rows),
      collected > 0 && h('p', { class: 'map-total' }, `${plural(collected, 'record')} to screen, each counted once`),
    ];
  }
  if (key === 'screen') {
    const decided = (d) => papers.filter((p) => p.screen?.decision === d).length;
    const waiting = papers.filter((p) => p.status === 'candidate').length;
    const reviews = papers.filter((p) => p.screen?.decision === 'include' && p.screen.study_type === 'systematic_review_or_meta_analysis').length;
    return [
      bar([
        { kind: 'include', label: 'Included', n: decided('include') },
        { kind: 'unsure', label: 'Unsure', n: decided('unsure') },
        { kind: 'exclude', label: 'Excluded', n: decided('exclude') },
        { kind: 'waiting', label: 'Waiting', n: waiting },
      ]),
      reviews > 0 && h('p', { class: 'map-note' }, `${plural(reviews, 'review')} among those included, whose references are followed next.`),
    ];
  }
  if (key === 'follow') {
    const e = log.find((x) => x.level === 'cited');
    if (!e) return null;
    if (!e.reviews) return h('p', { class: 'map-note' }, 'No review passed screening, so there were no references to follow.');
    return facts(`${plural(e.reviews, 'review')} followed`, `${plural(e.cited, 'reference')} in PubMed`,
      `${number(e.already)} already found`, `${number(e.not_studies ?? 0)} not studies (reviews, guidelines, letters)`,
      `${plural(e.added, 'study', 'studies')} added and screened`, e.failed?.length && `${e.failed.length} could not be read`);
  }
  if (key === 'extract') {
    const kept = papers.filter((p) => KEPT.has(p.status));
    if (!kept.length) return null;
    const count = (f) => kept.filter(f).length;
    const kinds = [['registry', 'Registry results'], ['pmc_xml', 'PMC full text'], ['oa_pdf', 'Open-access PDF'], ['abstract', 'Abstract only']];
    const fetched = count((p) => roleOf(p.fetched_by) === 'fetcher');
    const redone = count((p) => p.extracted_by === 'manager');
    return [
      bar([
        { kind: 'include', label: 'Extracted', n: count((p) => p.status === 'extracted') },
        { kind: 'active', label: 'Reading', n: count((p) => p.status === 'fetching' || p.status === 'extracting') },
        { kind: 'unsure', label: 'Needs attention', n: count((p) => ['no_fulltext', 'needs_manual', 'error'].includes(p.status)) },
        { kind: 'waiting', label: 'Waiting', n: count((p) => p.status === 'screened_in' || p.status === 'unsure') },
      ]),
      facts(
        ...kinds.map(([k, label]) => [count((p) => p.fulltext_kind === k), label]).filter(([n]) => n).map(([n, label]) => `${label}: ${number(n)}`),
        fetched > 0 && `${plural(fetched, 'document')} downloaded by ${names.fetcher || 'Claude'}`,
        redone > 0 && `${plural(redone, 'paper')} re-extracted by ${names.manager}`,
      ),
    ];
  }
  if (key === 'overlap') {
    if (state === 'pending') return null;
    const overlap = project.overlap;
    if (overlap?.error) return h('p', { class: 'map-note' }, `The check failed: ${overlap.error}. The summary tries it again.`);
    if (!overlap?.report) return null;
    const groups = overlap.report.groups;
    const by = (r) => groups.filter((g) => g.relation === r).length;
    return groups.length
      ? facts(by('same_study') && `${plural(by('same_study'), 'study', 'studies')} reported more than once`,
        by('review_includes_study') && `${plural(by('review_includes_study'), 'review')} pooling studies in the set`,
        by('reviews_share_studies') && `${plural(by('reviews_share_studies'), 'pair')} of reviews sharing studies`,
        by('possible_overlap') && `${plural(by('possible_overlap'), 'possible overlap')}`)
      : h('p', { class: 'map-note' }, 'No overlap found.');
  }
  if (key === 'ready') {
    const extracted = papers.filter((p) => p.status === 'extracted').length;
    return extracted ? facts(`${plural(extracted, 'record')} extracted, ready for review`) : null;
  }
  return null;
}

const STATE_TEXT = { done: 'Done', active: 'Running', stopped: 'Stopped here', pending: 'Waiting' };

// The map of a search. `running` says whether it is running now; `fetcher` whether a model downloads
// what the browser may not (the browser edition with Claude; the local server downloads by itself);
// `names` are the roles' names, e.g. { manager: 'Opus 5.5', worker: 'Haiku 5.5', ... } (providers.js).
export function searchMap({ project, papers, running, fetcher = false, names = {} }) {
  names = { ...ROLE_NAMES, ...names };
  const states = stageStates(project, running);
  const progress = project.progress || {};
  const log = project.search_log || [];
  return h('ol', { class: 'map-stages' }, STAGES.map((stage, i) => {
    const state = states[stage.key];
    const live = state === 'active' && STAGE_OF[progress.stage] === stage.key && progress.total
      ? `${STATE_TEXT.active}: ${number(progress.done)} of ${number(progress.total)}` : STATE_TEXT[state];
    return h('li', { class: 'map-stage', 'data-stage': stage.key, 'data-state': state },
      h('span', { class: 'map-dot', 'aria-hidden': 'true' }, state === 'done' ? '✓' : String(i + 1)),
      h('div', { class: 'map-card' },
        h('div', { class: 'map-head' },
          h('h4', {}, stage.title),
          h('span', { class: 'map-agents' }, stage.agents.filter((a) => fetcher || a !== 'fetcher')
            .map((a) => h('span', { class: 'map-agent', 'data-agent': a }, names[a]))),
          h('span', { class: 'map-state' }, live)),
        h('p', { class: 'map-how' }, stage.how(names)),
        details(stage.key, { project, papers, state, progress, log, names })));
  }));
}
