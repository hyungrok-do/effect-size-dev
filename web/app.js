// Effect Size Finder: the page. Talks to the JSON API in python/esf/server.py.

import { overlapNote, resultBox, reviewKeys, reviewNav, sourceUrl, summaryView } from '/lib/review-view.js';
import { searchMap } from '/lib/search-map.js';
import { SpecPanel } from '/lib/spec-panel.js';

const RUNNING = new Set(['reading', 'searching', 'screening', 'extracting']);
const TEXT_KIND = {
  registry: 'Registry results', pmc_xml: 'PMC full text', oa_pdf: 'Open-access PDF', abstract: 'Abstract only',
};
const TABS = [
  ['extracted', 'Extracted'],
  ['attention', 'Needs attention'],
  ['pending', 'In progress'],
  ['excluded', 'Screened out'],
];

const $ = (selector) => document.querySelector(selector);
const state = {
  id: null, project: null, papers: [], tab: 'extracted', timer: null, specKey: '', papersKey: '', config: {},
  reviewAt: {}, // tab -> the paper id shown there
};
const KEY_STORAGE = 'esf-anthropic-api-key';

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

// -- API key ---------------------------------------------------------------------
// Kept in this tab (sessionStorage) or, if asked, on this browser (localStorage). It is sent only
// with the requests that start Claude work.

function storage(kind) {
  try {
    return kind === 'local' ? window.localStorage : window.sessionStorage;
  } catch {
    return null;
  }
}

function loadKey() {
  const remembered = storage('local')?.getItem(KEY_STORAGE);
  $('#api-key').value = remembered || storage('session')?.getItem(KEY_STORAGE) || '';
  $('#remember-key').checked = Boolean(remembered);
}

function saveKey() {
  const key = apiKey();
  const remember = $('#remember-key').checked;
  try {
    storage('local')?.removeItem(KEY_STORAGE);
    storage('session')?.removeItem(KEY_STORAGE);
    if (key) storage(remember ? 'local' : 'session')?.setItem(KEY_STORAGE, key);
  } catch { /* storage unavailable: the key lives only in the field */ }
  showKeyStatus();
}

function forgetKey() {
  $('#api-key').value = '';
  $('#remember-key').checked = false;
  saveKey();
  $('#api-key').focus();
}

const apiKey = () => $('#api-key').value.trim();

function showKeyStatus() {
  const box = $('.keybox');
  const status = $('#key-status');
  box.dataset.missing = 'false';
  if (apiKey()) {
    status.textContent = $('#remember-key').checked
      ? 'Remembered on this browser. Sent only with requests that run Claude; the server does not store it.'
      : 'Kept for this tab only. Sent only with requests that run Claude; the server does not store it.';
  } else if (state.config.server_has_key) {
    status.textContent = 'Optional: the server has its own key.';
  } else {
    status.replaceChildren('Needed to read the problem, search, and extract. Create one in the ',
      h('a', { href: 'https://console.anthropic.com/settings/keys', target: '_blank', rel: 'noopener' }, 'Anthropic Console'), '.');
  }
}

function haveKey() {
  if (apiKey() || state.config.server_has_key) return true;
  $('.keybox').dataset.missing = 'true';
  $('#key-status').textContent = 'Enter your Anthropic API key first.';
  $('#api-key').focus();
  return false;
}

async function api(path, options = {}) {
  const { withKey, ...rest } = options;
  const init = { ...rest, headers: { 'Content-Type': 'application/json' } };
  if (withKey && apiKey()) init.headers['X-Anthropic-Api-Key'] = apiKey();
  if (init.body !== undefined && typeof init.body !== 'string') init.body = JSON.stringify(init.body);
  const response = await fetch(path, init);
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail);
    } catch { /* not JSON */ }
    throw new Error(detail);
  }
  const type = response.headers.get('content-type') || '';
  return type.includes('json') ? response.json() : response.text();
}

function showError(el, message) {
  el.textContent = message || '';
  el.hidden = !message;
}

const statusLabel = (status) => ({
  new: 'new', reading: 'reading the problem', spec_ready: 'ready to search', searching: 'searching',
  screening: 'screening', extracting: 'extracting', done: 'done', error: 'stopped with an error',
  interrupted: 'interrupted',
}[status] || status);

// -- projects list ---------------------------------------------------------------

async function loadProjects() {
  const list = $('#project-list');
  let projects;
  try {
    projects = await api('/api/projects');
  } catch {
    return;
  }
  if (!projects.length) {
    list.replaceChildren(h('li', { class: 'muted small' }, 'None yet.'));
    return;
  }
  list.replaceChildren(...projects.map((p) => {
    const text = p.paragraph.length > 90 ? `${p.paragraph.slice(0, 90)}…` : p.paragraph;
    return h('li', {}, h('button', {
      type: 'button', class: 'project-link', 'aria-current': p.id === state.id ? 'true' : null,
      onclick: () => openProject(p.id),
    }, text, h('small', {}, `${new Date(p.created_at).toLocaleString()} · ${statusLabel(p.status)}`)));
  }));
}

function newSearch() {
  clearTimeout(state.timer);
  Object.assign(state, { id: null, project: null, papers: [], specKey: '', papersKey: '' });
  history.replaceState(null, '', location.pathname);
  const paragraph = $('#paragraph');
  paragraph.value = '';
  paragraph.readOnly = false;
  $('#read').hidden = false;
  $('#read-status').textContent = '';
  showError($('#problem-error'), '');
  for (const id of ['#spec', '#progress-card', '#review']) $(id).hidden = true;
  loadProjects();
  paragraph.focus();
}

async function openProject(id) {
  clearTimeout(state.timer);
  Object.assign(state, { id, project: null, papers: [], specKey: '', papersKey: '', reviewAt: {} });
  history.replaceState(null, '', `#${id}`);
  try {
    await refresh();
  } catch (error) {
    showError($('#problem-error'), `Could not open this search: ${error.message}`);
  }
  loadProjects();
}

async function refresh() {
  if (!state.id) return;
  const id = state.id;
  const project = await api(`/api/projects/${id}`);
  if (id !== state.id) return;
  const wasRunning = state.project && RUNNING.has(state.project.status);
  state.project = project;
  const hasPapers = Object.keys(project.counts || {}).length > 0;
  if (hasPapers) state.papers = await api(`/api/projects/${id}/papers`);
  if (id !== state.id) return;
  renderProblem(project);
  renderSpec(project);
  renderProgress(project);
  if (hasPapers) renderReview();
  else $('#review').hidden = true;
  renderSummary(project);
  const extracting = state.papers.some((p) => p.status === 'fetching' || p.status === 'extracting');
  if (RUNNING.has(project.status) || extracting || project.summary?.status === 'running') {
    clearTimeout(state.timer);
    state.timer = setTimeout(() => refresh().catch(() => {}), 2500);
  } else if (wasRunning) {
    loadProjects();
  }
}

// -- step 1 ----------------------------------------------------------------------

function renderProblem(project) {
  const paragraph = $('#paragraph');
  paragraph.value = project.paragraph;
  paragraph.readOnly = true;
  $('#read').hidden = true;
  $('#read-status').textContent = project.status === 'reading' ? 'Opus is reading the problem…' : '';
  showError($('#problem-error'), !project.spec && project.error ? project.error : '');
}

async function readProblem() {
  const text = $('#paragraph').value.trim();
  if (text.length < 20) {
    showError($('#problem-error'), 'Describe the study in a sentence or two first.');
    return;
  }
  if (!haveKey()) return;
  $('#read').disabled = true;
  try {
    const { id } = await api('/api/projects', { method: 'POST', body: { paragraph: text }, withKey: true });
    await openProject(id);
  } catch (error) {
    showError($('#problem-error'), error.message);
  } finally {
    $('#read').disabled = false;
  }
}

// -- step 2 ----------------------------------------------------------------------

// The specification, as structured tables edited one part at a time (shared with the browser
// edition); an edit is saved with the search.
const specPanel = new SpecPanel($('#spec-panel'), {
  onSave: async (spec) => {
    state.specKey = JSON.stringify(spec); // already shown: the redraw after saving leaves the panel as it is
    try {
      await api(`/api/projects/${state.id}/spec`, { method: 'PUT', body: spec });
    } catch (error) {
      $('#run-status').textContent = error.message;
    }
  },
});

function renderSpec(project) {
  const card = $('#spec');
  if (!project.spec) {
    card.hidden = true;
    return;
  }
  card.hidden = false;
  const key = JSON.stringify(project.spec);
  if (key !== state.specKey) {
    state.specKey = key;
    specPanel.show(project.spec);
  }
  const running = RUNNING.has(project.status);
  specPanel.disabled = running;
  $('#run').disabled = running;
  const hasPapers = Object.keys(project.counts || {}).length > 0;
  $('#run').textContent = hasPapers ? 'Search again' : 'Search and extract';
  $('#run-status').textContent = hasPapers && !running ? 'Searching again replaces the papers and decisions below.' : '';
}

async function runSearch(event) {
  event.preventDefault();
  const hasPapers = Object.keys(state.project.counts || {}).length > 0;
  if (!haveKey()) return;
  const spec = specPanel.collect();
  if (!spec) return; // an open edit has a value to fix; the panel says which
  if (hasPapers && !confirm('Search again? This replaces the papers and your decisions for this search.')) return;
  $('#run').disabled = true;
  try {
    await api(`/api/projects/${state.id}/spec`, { method: 'PUT', body: spec });
    await api(`/api/projects/${state.id}/run`, { method: 'POST', withKey: true });
    state.papersKey = '';
    await refresh();
    $('#progress-card').scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (error) {
    $('#run-status').textContent = error.message;
  } finally {
    $('#run').disabled = false;
  }
}

// -- step 3 ----------------------------------------------------------------------

function renderProgress(project) {
  const card = $('#progress-card');
  const counts = project.counts || {};
  const started = RUNNING.has(project.status) && project.status !== 'reading';
  card.hidden = !(started || Object.keys(counts).length || project.search_plan || (project.spec && project.error));
  if (card.hidden) return;

  $('#map-body').replaceChildren(searchMap({ project, papers: state.papers, running: RUNNING.has(project.status) }));

  const progress = project.progress || {};
  const bar = $('#progress');
  bar.hidden = !RUNNING.has(project.status);
  bar.max = progress.total || 1;
  bar.value = progress.total ? progress.done : 0;

  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  const kept = total - (counts.screened_out || 0) - (counts.candidate || 0);
  const parts = [];
  if (RUNNING.has(project.status) && progress.stage) {
    parts.push(progress.total ? `${progress.stage}: ${progress.done} of ${progress.total}` : `${progress.stage}…`);
  }
  if (total) parts.push(`${total} records found`, `${kept} kept after screening`, `${counts.extracted || 0} extracted`);
  if (project.status === 'interrupted') parts.push('interrupted by a server restart; search again to finish');
  $('#progress-text').textContent = parts.join(' · ');
  showError($('#run-error'), project.spec && project.error ? project.error : '');

  const plan = project.search_plan;
  $('#plan-details').hidden = !plan;
  if (plan) {
    // The search runs from the top down; each line says what its query found (from the search log).
    const names = project.level_names || {};
    const order = Object.keys(names); // the levels from the top down
    const log = project.search_log || [];
    const count = (n, one, many = `${one}s`) => `${Number(n).toLocaleString()} ${n === 1 ? one : many}`;
    const found = (level, query) => {
      const e = log.find((x) => x.level === level && x.query === query);
      if (!e) return '';
      return e.error ? ` (failed: ${e.error})` : ` (${count(e.hits, 'hit')}; ${count(e.added, 'new record')} screened)`;
    };
    const levels = [...(plan.levels || (plan.pubmed_queries || []).map((query) => ({ level: 'primary', query, why: '' })))]
      .sort((a, b) => order.indexOf(a.level) - order.indexOf(b.level));
    const cited = log.find((e) => e.level === 'cited');
    $('#plan-summary').textContent = plan.summary;
    $('#plan-queries').replaceChildren(
      ...levels.map((l) => h('li', {}, `${names[l.level] || l.level}: `, h('code', {}, l.query), found(l.level, l.query))),
      ...(cited ? [h('li', {}, `${names.cited || 'Cited by the included reviews'}: ${count(cited.reviews, 'review')} followed, `
        + `${count(cited.cited, 'record')} cited in PubMed, ${count(cited.added, 'new record')} screened`)] : []),
      ...plan.clinicaltrials_queries.map((q) => h('li', {}, 'ClinicalTrials.gov: ', h('code', {}, q), found('registry', q))),
      ...(plan.extra_ids.length ? [h('li', {}, `Named records: ${plan.extra_ids.join(', ')}`)] : []),
      ...(plan.failed_queries || []).map((q) => h('li', {}, badge('Failed', 'err'), ' ', q)),
    );
  }
  const usage = project.usage || [];
  $('#usage-details').hidden = !usage.length;
  $('#usage').replaceChildren(...usage.map((u) => h('tr', {},
    h('td', {}, u.step), h('td', {}, u.model), h('td', { class: 'num' }, u.calls),
    h('td', { class: 'num' }, u.input_tokens.toLocaleString()), h('td', { class: 'num' }, u.output_tokens.toLocaleString()),
    h('td', { class: 'num' }, u.cache_read_input_tokens.toLocaleString()))));
}

// -- step 4 ----------------------------------------------------------------------

function groupOf(paper) {
  const running = RUNNING.has(state.project.status);
  if (paper.status === 'extracted') return 'extracted';
  if (paper.status === 'screened_out') return 'excluded';
  if (['fetching', 'extracting'].includes(paper.status)) return 'pending';
  if (running && ['candidate', 'screened_in', 'unsure'].includes(paper.status)) return 'pending';
  return 'attention';
}

// The review shows one paper at a time: a progress line, a chip per paper to jump to, and the paper.
function renderReview() {
  const key = JSON.stringify([state.papers.map((p) => [p.id, p.status, p.decisions, p.extracted_by]), state.tab, state.project.status,
    state.reviewAt, state.project.overlap]);
  if (key === state.papersKey) return;
  state.papersKey = key;
  $('#review').hidden = false;

  const groups = Object.fromEntries(TABS.map(([k]) => [k, []]));
  for (const paper of state.papers) groups[groupOf(paper)].push(paper);
  $('#tabs').replaceChildren(...TABS.map(([k, label]) => h('button', {
    type: 'button', class: 'secondary small', 'aria-pressed': String(state.tab === k),
    onclick: () => { state.tab = k; state.papersKey = ''; renderReview(); },
  }, `${label} (${groups[k].length})`)));

  const base = `/api/projects/${state.id}`;
  $('#export-included').href = `${base}/export.csv?included_only=true`;
  $('#export-all').href = `${base}/export.csv`;
  $('#export-json').href = `${base}/export.json`;

  const trials = new Map();
  for (const paper of state.papers) {
    for (const nct of paper.nct_ids || []) trials.set(nct, (trials.get(nct) || 0) + 1);
  }
  const current = reviewNav({
    papers: groups[state.tab], current: state.reviewAt[state.tab], decisionsOf: (p) => p.decisions || {},
    show: showPaper, counting: state.tab === 'extracted',
  });
  $('#papers').replaceChildren(current ? paperCard(current, trials) : h('p', { class: 'muted' }, 'Nothing here.'));
}

function showPaper(paperId) {
  state.reviewAt[state.tab] = paperId;
  renderReview();
  $('#papers .paper h3')?.scrollIntoView({ block: 'nearest' });
}

function badge(text, kind = '') {
  return h('span', { class: `badge ${kind}`.trim() }, text);
}

// A paper for review, kept short: its title (linked to where it was found), key, authors and year,
// warnings only when there is something to watch, and each result as a line per group. Everything
// else (how it was found, the text used, which model extracted it and why) is under Details.
function paperCard(paper, trials) {
  const decisions = paper.decisions || {};
  const paperDecision = decisions['-1']?.decision;
  const sharedTrial = (paper.nct_ids || []).find((nct) => trials.get(nct) > 1);
  const running = RUNNING.has(state.project.status);
  const extracted = paper.status === 'extracted' && paper.extraction;
  const url = sourceUrl(paper, paper.links || {});
  const title = paper.title || paper.ext_id;
  const overlap = overlapNote(paper, state.project.overlap?.report, state.papers);
  const differs = extracted && !paper.extraction.matches_spec && paper.extraction.mismatch_reasons.length > 0;

  const head = [
    h('h3', {}, url ? h('a', { href: url, target: '_blank', rel: 'noopener' }, title) : title),
    h('div', { class: 'meta' },
      badge(`R${paper.id}`, 'accent'),
      paper.authors && h('span', {}, paper.authors),
      paper.year && h('span', {}, paper.year),
      paper.ext_id.startsWith('PPR') && badge('Preprint, not peer reviewed', 'warn'),
      paper.fulltext_kind === 'abstract' && badge('Abstract only', 'warn'),
      differs && badge('Differs from the plan', 'warn'),
      !overlap && sharedTrial && badge(`Another record shares ${sharedTrial}`, 'warn')),
    overlap,
  ];

  const screen = paper.screen;
  const reasons = paper.escalation?.reasons || [];
  const details = h('details', { class: 'paper-details' }, h('summary', {}, 'Details'),
    h('div', { class: 'meta' },
      paper.journal && h('span', {}, paper.journal),
      badge({ clinicaltrials: 'ClinicalTrials.gov', pubmed: 'PubMed' }[paper.source] || 'Europe PMC'),
      paper.fulltext_kind && badge(TEXT_KIND[paper.fulltext_kind] || paper.fulltext_kind),
      paper.extracted_by === 'manager' && badge('Reviewed by Opus', 'accent'),
      paper.extracted_by === 'worker' && badge('Extracted by Haiku'),
      paper.escalation?.audit && badge('Audit sample'),
      ...Object.entries(paper.links || {}).map(([label, href]) => h('a', { href, target: '_blank', rel: 'noopener' }, label))),
    extracted && screen && h('p', { class: 'reason' }, `Screening (${screen.decision}): ${screen.reason}`),
    reasons.length > 0 && h('p', { class: 'reason' }, `Why Opus reviewed it: ${reasons.join('; ')}`),
    differs && h('p', { class: 'reason' }, `Differs from the plan: ${paper.extraction.mismatch_reasons.join('; ')}`),
    extracted && paper.extraction.notes && h('p', { class: 'reason' }, `Notes: ${paper.extraction.notes}`));

  // What to do with it: decide on an extracted paper; get the text of one that has none yet.
  const actions = h('div', { class: 'paper-actions' });
  if (extracted) {
    actions.append(h('button', {
      type: 'button', class: 'secondary small', 'aria-pressed': String(paperDecision === 'exclude'),
      onclick: () => decide(paper, -1, paperDecision === 'exclude' ? null : 'exclude', ''),
    }, paperDecision === 'exclude' ? 'Undo excluding the paper' : 'Exclude the whole paper'));
  }
  if (paper.fulltext_kind) {
    actions.append(h('button', { type: 'button', class: 'secondary small', onclick: () => showSource(paper) }, 'Show source text'));
  }
  if (!running && ['screened_out', 'no_fulltext', 'needs_manual', 'screened_in', 'unsure', 'candidate'].includes(paper.status)) {
    actions.append(h('button', { type: 'button', class: 'secondary small', onclick: () => extractAnyway(paper) },
      paper.status === 'screened_out' ? 'Extract anyway' : 'Try extracting again'));
  }

  const body = [];
  if (extracted) {
    if (!paper.extraction.results.length) body.push(h('p', { class: 'reason muted' }, 'No relevant results were reported.'));
    paper.extraction.results.forEach((result, i) => body.push(
      resultBox(paper, result, i, decisions, (index, decision, reason) => decide(paper, index, decision, reason))));
  } else {
    // Not extracted: why, which is what matters here.
    if (screen) body.push(h('p', { class: 'reason' }, `Screening (${screen.decision}): ${screen.reason}`));
    if (paper.error) body.push(h('p', { class: 'reason' }, badge('Note', 'warn'), ' ', paper.error));
  }
  return h('article', { class: 'paper' }, head, body, actions.childElementCount ? actions : null, details);
}

async function decide(paper, resultIndex, decision, reason) {
  try {
    await api(`/api/projects/${state.id}/papers/${paper.id}/decisions`, {
      method: 'PUT', body: { result_index: resultIndex, decision, reason },
    });
    const key = String(resultIndex);
    paper.decisions = { ...(paper.decisions || {}) };
    if (decision) paper.decisions[key] = { decision, reason };
    else delete paper.decisions[key];
    state.papersKey = '';
    renderReview();
    renderSummary(state.project);
  } catch (error) {
    alert(`Could not save the decision: ${error.message}`);
  }
}

async function extractAnyway(paper) {
  if (!haveKey()) return;
  try {
    await api(`/api/projects/${state.id}/papers/${paper.id}/extract`, { method: 'POST', withKey: true });
    state.tab = 'pending';
    state.papersKey = '';
    await refresh();
  } catch (error) {
    alert(`Could not start the extraction: ${error.message}`);
  }
}

// -- source text -------------------------------------------------------------------

function quoteRanges(text, quotes) {
  const ranges = [];
  for (const quote of quotes) {
    const words = quote.split(/\s+/).filter(Boolean).map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
    if (!words.length) continue;
    const match = new RegExp(words.join('\\s+'), 'i').exec(text);
    if (match) ranges.push([match.index, match.index + match[0].length]);
  }
  ranges.sort((a, b) => a[0] - b[0]);
  const merged = [];
  for (const range of ranges) {
    const last = merged[merged.length - 1];
    if (last && range[0] <= last[1]) last[1] = Math.max(last[1], range[1]);
    else merged.push([...range]);
  }
  return merged;
}

async function showSource(paper) {
  const dialog = $('#source-dialog');
  const pre = $('#source-text');
  $('#source-title').textContent = paper.title || 'Source text';
  pre.textContent = 'Loading…';
  dialog.showModal();
  let text;
  try {
    text = await api(`/api/projects/${state.id}/papers/${paper.id}/source`);
  } catch (error) {
    pre.textContent = error.message;
    return;
  }
  const quotes = (paper.extraction?.results || []).flatMap((r) => r.values.map((v) => v.quote));
  const nodes = [];
  let at = 0;
  for (const [start, end] of quoteRanges(text, quotes)) {
    nodes.push(text.slice(at, start), h('mark', {}, text.slice(start, end)));
    at = end;
  }
  nodes.push(text.slice(at));
  pre.replaceChildren(...nodes);
  if (paper.fulltext_kind === 'oa_pdf') {
    pre.prepend(h('span', { class: 'muted' }, 'Text layer of the PDF; tables may be out of order.\n\n'));
  }
  pre.querySelector('mark')?.scrollIntoView({ block: 'center' });
}

// -- step 5: the summary -------------------------------------------------------------

function renderSummary(project) {
  const summary = project.summary;
  const running = summary?.status === 'running';
  const searching = RUNNING.has(project.status);
  $('#summary').hidden = !state.papers.some((p) => p.status === 'extracted') && !summary;
  $('#summarize').disabled = running || searching;
  $('#summarize').textContent = summary?.status === 'done' ? 'Write the summary again' : 'Write the summary';
  $('#summarize-status').textContent = running ? 'Opus is checking for overlap and writing the summary…'
    : searching ? 'Wait until the search finishes.' : '';
  showError($('#summary-error'), summary?.status === 'error' ? summary.error : '');
  const decisions = Object.fromEntries(state.papers.map((p) => [p.id, p.decisions || {}]));
  $('#summary-body').replaceChildren(...(summary?.status === 'done'
    ? summaryView({ summary, overlap: project.overlap?.report, papers: state.papers, decisions, linksOf: (p) => p.links })
    : running ? [] : [h('p', { class: 'muted' },
      'Review the papers first: the summary uses every result you included or left undecided, and none you excluded.')]));
}

async function writeSummary() {
  if (!haveKey() || !state.id) return;
  showError($('#summary-error'), '');
  $('#summarize').disabled = true;
  try {
    await api(`/api/projects/${state.id}/summary`, { method: 'POST', withKey: true });
  } catch (error) {
    showError($('#summary-error'), error.message);
  }
  await refresh();
}

// -- start -------------------------------------------------------------------------

async function start() {
  $('#read').addEventListener('click', readProblem);
  $('#spec-form').addEventListener('submit', runSearch);
  $('#new-project').addEventListener('click', newSearch);
  $('#source-close').addEventListener('click', () => $('#source-dialog').close());
  $('#api-key').addEventListener('input', saveKey);
  $('#remember-key').addEventListener('change', saveKey);
  $('#forget-key').addEventListener('click', forgetKey);
  $('#summarize').addEventListener('click', writeSummary);
  document.addEventListener('keydown', reviewKeys);
  loadKey();
  try {
    const config = await api('/api/config');
    state.config = config;
    $('#models').textContent = `Manager ${config.manager_model} · workers ${config.worker_model}`
      + (config.contact_email_set ? '' : ' · set ESF_CONTACT_EMAIL to fetch open-access PDFs');
  } catch {
    $('#models').textContent = 'The server is not responding.';
  }
  showKeyStatus();
  await loadProjects();
  const id = location.hash.slice(1);
  if (id) await openProject(id);
}

start();
