// Effect Size Finder, browser edition: the whole app runs in this page. Searches are kept in
// IndexedDB; Claude is called with the user's own key; nothing goes to a server of ours.

import { Activity, liveTasks, short } from './lib/activity.js';
import { Claude, looksLikeAnthropicKey, MODELS } from './lib/claude.js';
import { csvOf, download, exportRows, LOG_COLUMNS, logRows, PAPER, toCsv } from './lib/export.js';
import { LEVEL_NAMES, LEVELS } from './lib/generated.js';
import { Pipeline } from './lib/pipeline.js';
import { europepmcUrl, Sources } from './lib/sources.js';
import { decisionsBasis } from './lib/evidence.js';
import { foldable, overlapNote, paperHeader, resultBox, reviewKeys, reviewNav, summaryView } from './lib/review-view.js';
import { searchMap } from './lib/search-map.js';
import { SpecPanel } from './lib/spec-panel.js';
import { RUNNING, Store } from './lib/store.js';

const TEXT_KIND = {
  registry: 'Registry results', pmc_xml: 'PMC full text', oa_pdf: 'Open-access PDF', abstract: 'Abstract only', upload: 'Your PDF',
};
const TABS = [
  ['extracted', 'Extracted'],
  ['attention', 'Needs attention'],
  ['pending', 'In progress'],
  ['excluded', 'Screened out'],
];
const STEPS = ['setup', 'problem', 'spec', 'progress', 'review', 'summary'];
const PANELS = { setup: '#setup', problem: '#problem', spec: '#spec', progress: '#progress-card', review: '#review', summary: '#summary' };
// Papers (from PubMed and Europe PMC) and registry records are reviewed separately.
const SOURCES = [['papers', 'Papers'], ['clinicaltrials', 'ClinicalTrials.gov']];
const kindOf = (paper) => (paper.source === 'clinicaltrials' ? 'clinicaltrials' : 'papers');
// Which search found a paper, on its card; the search log names each level in full (LEVEL_NAMES).
const LEVEL_SHORT = {
  reviews: 'Reviews search', primary: 'Target-population search', full_text: 'Full-text search',
  wider: 'Related-populations search', outcome_data: 'Outcome-data search', cited: 'Cited by a review', named: 'Named by Opus',
};
const version = (model) => model.replace(/^claude-[a-z]+-/, '').replace('-', '.');
const AGENTS = [
  ['opus', `Opus ${version(MODELS.manager)}`, 'manager', 'Reads the problem, plans the search, reviews flagged extractions'],
  ['sonnet', `Sonnet ${version(MODELS.fetcher)}`, 'fetcher', 'Downloads what the browser may not read'],
  ['haiku', `Haiku ${version(MODELS.worker)}`, 'workers', 'Screen every record and extract every paper'],
  ['code', 'Code', 'search and checks', 'Runs the searches, gets the text, checks every quote'],
];
const AGENT_NAMES = Object.fromEntries(AGENTS.map(([key, name]) => [key, name]));
const ACTIVITY_OPEN = 'esf-activity-open';
const KEY = 'esf-anthropic-api-key';
const EMAIL = 'esf-contact-email';
const NCBI_KEY = 'esf-ncbi-api-key';

const $ = (selector) => document.querySelector(selector);
const plural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;
const store = new Store();
const jobs = new Set(); // projects and papers with work running in this tab
const state = {
  id: null, project: null, papers: [], decisions: {}, usage: [], events: [], tab: 'extracted', specKey: '', papersKey: '',
  uploadFor: null, step: 'setup', stepChosen: false, source: 'papers', filter: 'all', timelineAll: false,
  available: { setup: true, problem: true, spec: false, progress: false, review: false, summary: false },
  reviewAt: {}, // `${source}:${tab}` -> the paper id shown there
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

function showError(el, message) {
  el.textContent = message || '';
  el.hidden = !message;
}

const statusLabel = (status) => ({
  new: 'new', reading: 'reading the problem', spec_ready: 'ready to search', searching: 'searching',
  screening: 'screening', extracting: 'extracting', done: 'done', error: 'stopped with an error',
  interrupted: 'stopped before it finished',
}[status] || status);

// -- settings: kept in this tab (sessionStorage) or, if asked, this browser (localStorage) -----

function storage(kind) {
  try {
    return kind === 'local' ? window.localStorage : window.sessionStorage;
  } catch {
    return null;
  }
}

function readSetting(name) {
  try {
    return storage('local')?.getItem(name) || '';
  } catch {
    return '';
  }
}

function writeSetting(name, value) {
  try {
    if (value) storage('local')?.setItem(name, value);
    else storage('local')?.removeItem(name);
  } catch { /* storage unavailable: the value lives only in the field */ }
}

function loadSettings() {
  const remembered = storage('local')?.getItem(KEY);
  $('#api-key').value = remembered || storage('session')?.getItem(KEY) || '';
  $('#remember-key').checked = Boolean(remembered);
  $('#contact-email').value = readSetting(EMAIL);
  $('#ncbi-key').value = readSetting(NCBI_KEY);
}

const apiKey = () => $('#api-key').value.trim();

function saveKey() {
  const key = apiKey();
  try {
    storage('local')?.removeItem(KEY);
    storage('session')?.removeItem(KEY);
    if (key) storage($('#remember-key').checked ? 'local' : 'session')?.setItem(KEY, key);
  } catch { /* storage unavailable */ }
  showKeyStatus();
}

function forgetKey() {
  $('#api-key').value = '';
  $('#remember-key').checked = false;
  saveKey();
  $('#api-key').focus();
}

function showKeyStatus() {
  $('.keybox').dataset.missing = 'false';
  $('#setup-flag').hidden = Boolean(apiKey());
  const status = $('#key-status');
  if (apiKey() && !looksLikeAnthropicKey(apiKey())) {
    $('.keybox').dataset.missing = 'true';
    status.textContent = "This doesn't look like an Anthropic API key: those start with sk-ant-. "
      + 'An NCBI key goes in the NCBI API key box below.';
  } else if (apiKey()) {
    status.textContent = $('#remember-key').checked
      ? 'Remembered on this browser. It is sent only to Anthropic, straight from this page.'
      : 'Kept for this tab only. It is sent only to Anthropic, straight from this page.';
  } else {
    status.replaceChildren('Needed to read the problem, search, and extract. Create one in the ',
      h('a', { href: 'https://console.anthropic.com/settings/keys', target: '_blank', rel: 'noopener' }, 'Anthropic Console'), '.');
  }
}

async function checkKey() {
  if (!haveKey()) return;
  const button = $('#check-key');
  const status = $('#key-status');
  button.disabled = true;
  status.textContent = 'Checking the key with Anthropic…';
  try {
    const results = await new Claude({ apiKey: apiKey() }).checkAccess();
    const failed = results.filter((r) => !r.ok);
    $('.keybox').dataset.missing = String(failed.length > 0);
    status.textContent = failed.length
      ? failed.map((r) => `${r.model}: ${r.error}`).join(' ')
      : `The key works, and it may use ${results.map((r) => r.name).join(', ')}.`;
  } catch (error) {
    $('.keybox').dataset.missing = 'true';
    status.textContent = `Could not reach Anthropic: ${error?.message || error}`;
  } finally {
    button.disabled = false;
  }
}

// The jobs need a key; without one, the page goes to Setup and says so.
function haveKey() {
  if (apiKey()) return true;
  selectStep('setup');
  $('.keybox').dataset.missing = 'true';
  $('#key-status').textContent = 'Enter your Anthropic API key first.';
  $('#api-key').focus();
  return false;
}

// A pipeline wired to the settings as they are when a job starts.
// The literature sources alone, for work that needs no model (an older record's authors).
const literature = () => new Sources({ email: $('#contact-email').value.trim(), ncbiApiKey: $('#ncbi-key').value.trim() });

function pipeline(projectId) {
  const claude = new Claude({ apiKey: apiKey() });
  const activity = new Activity(store, projectId);
  const sources = new Sources({
    email: $('#contact-email').value.trim(),
    ncbiApiKey: $('#ncbi-key').value.trim(),
    proxy: (url) => activity.track(
      { agent: 'sonnet', step: 'fetch', text: `Fetching ${short(url.replace(/^https?:\/\//, ''), 70)} for the browser` },
      (step) => claude.fetchUrl(url, { sink: step.meter((callStep, model, usage) => store.addUsage(projectId, callStep, model, usage)) }),
      (doc) => (doc.kind === 'pdf' ? `PDF, ${Math.round((doc.base64.length * 3) / 4 / 1024)} KB` : `text, ${Math.round(doc.text.length / 1024)} KB`),
    ),
  });
  return new Pipeline({ store, claude, sources, activity });
}

// `then(projectId)` runs after the job, once the page shows its results: that is where the page
// moves on to the next step (the redraws are throttled, so watching statuses could miss a quick job).
async function startJob(key, work, then = null) {
  if (jobs.has(key)) return;
  jobs.add(key);
  try {
    await work();
  } finally {
    jobs.delete(key);
    await refreshNow();
    then?.();
    loadProjects();
  }
}

// After a job: go to `step` if the page is still on the search and on `from`.
const moveOn = (projectId, from, step) => () => {
  if (state.id === projectId && state.step === from) selectStep(step);
};

// -- projects list ---------------------------------------------------------------

async function loadProjects() {
  const list = $('#project-list');
  const projects = await store.projects();
  $('#points').hidden = projects.length > 0;
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
  Object.assign(state, {
    id: null, project: null, papers: [], decisions: {}, usage: [], events: [], specKey: '', papersKey: '',
    step: apiKey() ? 'problem' : 'setup', stepChosen: false,
    available: { setup: true, problem: true, spec: false, progress: false, review: false, summary: false },
  });
  history.replaceState(null, '', location.pathname);
  const paragraph = $('#paragraph');
  paragraph.value = '';
  paragraph.readOnly = false;
  $('#read').hidden = false;
  $('#read').textContent = 'Read the problem';
  $('#delete-project').hidden = true;
  $('#read-status').textContent = '';
  showError($('#problem-error'), '');
  renderTabs();
  renderActivity();
  loadProjects();
  if (state.step === 'problem') paragraph.focus();
}

async function openProject(id) {
  Object.assign(state, {
    id, project: null, papers: [], decisions: {}, usage: [], events: [], specKey: '', papersKey: '',
    stepChosen: false,
  });
  history.replaceState(null, '', `#${id}`);
  await refresh();
  if (!state.project) {
    newSearch();
    showError($('#problem-error'), 'That search is not stored in this browser.');
  }
  loadProjects();
}

async function deleteProject() {
  if (!state.id || jobs.has(state.id)) return;
  if (!confirm('Delete this search, its papers and your decisions from this browser?')) return;
  await store.deleteProject(state.id);
  newSearch();
}

let refreshing = null;
let again = false;

// Re-read the open search from storage and redraw. Calls during a redraw are folded into one more,
// which runs once this one has finished.
async function refresh() {
  if (refreshing) {
    again = true;
    return refreshing;
  }
  refreshing = (async () => {
    {
      again = false;
      if (!state.id) return;
      const id = state.id;
      const project = await store.project(id);
      if (id !== state.id) {
        again = true;
        return;
      }
      state.project = project;
      if (!project) return;
      project.counts = await store.statusCounts(id);
      state.papers = await store.papers(id);
      state.decisions = await store.decisions(id);
      state.usage = await store.usage(id);
      state.events = await store.events(id);
      renderProblem(project);
      renderSpec(project);
      renderProgress(project);
      state.available.review = state.papers.length > 0;
      if (state.available.review) renderReview();
      state.available.summary = state.papers.some((p) => p.status === 'extracted') || Boolean(project.summary);
      renderSummary(project);
      followTheWork(project);
      renderTabs();
      renderActivity();
    }
  })();
  try {
    await refreshing;
  } finally {
    refreshing = null;
    if (again) scheduleRefresh();
  }
}

// A redraw that starts after everything written so far: wait out one in progress, which may have
// read storage before the latest change.
async function refreshNow() {
  while (refreshing) await refreshing;
  await refresh();
}

// While a search runs, storage changes many times a second. Redraw at most every 0.4 s, so the page
// stays responsive and the pipeline isn't slowed by reading its own results back.
let pending = null;
let lastRefresh = 0;
function scheduleRefresh() {
  if (pending) return;
  pending = setTimeout(async () => {
    pending = null;
    lastRefresh = performance.now();
    await refresh();
  }, Math.max(0, 400 - (performance.now() - lastRefresh)));
}
store.addEventListener('change', (event) => {
  if (event.detail.projectId === state.id) scheduleRefresh();
});
store.addEventListener('live', (event) => {
  if (event.detail.projectId !== state.id) return;
  renderAgents();
  $('#tab-progress .live-dot').hidden = !jobs.has(state.id);
});

// -- steps as tabs -------------------------------------------------------------------

function defaultStep(project) {
  const searching = RUNNING.has(project.status) && project.status !== 'reading';
  if (searching || (['interrupted', 'error'].includes(project.status) && state.available.progress)) return 'progress';
  if (state.available.review) return 'review';
  if (state.available.spec) return 'spec';
  return 'problem';
}

// When a search is opened, show the step it has reached. After that the page only moves on when one
// of its own jobs finishes (see moveOn), so a step you picked yourself is left alone.
function followTheWork(project) {
  if (state.stepChosen) return;
  state.stepChosen = true;
  state.step = defaultStep(project);
}

function renderTabs() {
  if (!state.available[state.step]) {
    state.step = STEPS.slice(0, STEPS.indexOf(state.step) + 1).reverse().find((step) => state.available[step]) || 'problem';
  }
  for (const step of STEPS) {
    const tab = $(`#tab-${step}`);
    const selected = step === state.step;
    tab.setAttribute('aria-selected', String(selected));
    tab.setAttribute('aria-disabled', String(!state.available[step]));
    tab.tabIndex = selected ? 0 : -1;
    $(PANELS[step]).hidden = !selected;
  }
  $('#tab-progress .live-dot').hidden = !jobs.has(state.id);
}

function selectStep(step, focus = false) {
  if (!state.available[step]) return;
  state.step = step;
  renderTabs();
  if (focus) $(`#tab-${step}`).focus();
}

function tabKeys(event) {
  const open = STEPS.filter((step) => state.available[step]);
  const at = open.indexOf(state.step);
  const to = { ArrowRight: at + 1, ArrowLeft: at - 1, Home: 0, End: open.length - 1 }[event.key];
  if (to === undefined) return;
  event.preventDefault();
  selectStep(open[(to + open.length) % open.length], true);
}

// -- agent activity --------------------------------------------------------------------

const STEP_AGENT = {
  spec: 'opus', search: 'opus', adjudicate: 'opus', overlap: 'opus', summarize: 'opus', fetch: 'sonnet', screen: 'haiku', extract: 'haiku',
};
const seconds = (since) => `${Math.max(0, Math.round((Date.now() - since) / 1000))} s`;
const duration = (ms) => (ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(ms < 10000 ? 1 : 0)} s`);
const tokens = (n) => (n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${Math.round(n / 1e3)}k` : String(n));
let ticking = null;

// `remember` is false for the page's own default, so only a person's choice is kept.
function setActivityOpen(open, remember = true) {
  $('#activity').hidden = !open;
  $('.layout').classList.toggle('with-activity', open);
  $('#activity-toggle').setAttribute('aria-expanded', String(open));
  if (remember) writeSetting(ACTIVITY_OPEN, open ? 'open' : 'closed');
  renderActivity();
}

// Which agent the page's progress stage belongs to, for the progress line on its card.
const STAGE_AGENT = {
  'planning the search': 'opus', 'running the search': 'code', "reading the reviews' references": 'code', 'checking for overlap': 'opus',
  screening: 'haiku', extracting: 'haiku',
};
const MAX_JOBS = 8; // jobs listed per agent; the rest are counted

function jobRow(task, depth, children) {
  const u = task.usage;
  return [
    h('li', { class: 'job', 'data-depth': String(depth) },
      h('span', { class: 'what' }, task.text),
      h('span', { class: 'meta' }, seconds(task.started), u && ` · ${tokens(u.input)} in · ${tokens(u.output)} out`)),
    ...children.flatMap((child) => jobRow(child, depth + 1, [])),
  ];
}

// The agents' cards: what each is doing now, job by job, or the last job it finished.
function renderAgents() {
  const busy = state.id ? liveTasks(state.id) : [];
  $('#activity-toggle .pulse').hidden = busy.length === 0;
  if ($('#activity').hidden) return busy;
  const totals = {};
  for (const u of state.usage) {
    const agent = STEP_AGENT[u.step];
    if (!agent) continue;
    const t = (totals[agent] ||= { calls: 0, input: 0, output: 0 });
    t.calls += u.calls;
    t.input += u.input_tokens + (u.cache_read_input_tokens || 0) + (u.cache_creation_input_tokens || 0);
    t.output += u.output_tokens;
  }
  const codeSteps = state.events.filter((e) => e.agent === 'code').length;
  const progress = state.project?.progress;
  const running = jobs.has(state.id);
  const liveIds = new Set(busy.map((task) => task.id));
  $('#agents').replaceChildren(...(state.id ? AGENTS : []).map(([key, name, role, does]) => {
    const mine = busy.filter((task) => task.agent === key);
    // Jobs that are part of another live job are listed under it.
    const top = mine.filter((task) => !liveIds.has(task.parent));
    const childrenOf = (task) => busy.filter((child) => child.parent === task.id);
    const t = totals[key];
    const stats = key === 'code' ? `${codeSteps} steps logged`
      : t ? `${t.calls} calls · ${tokens(t.input)} in · ${tokens(t.output)} out` : 'No calls yet';
    const stage = running && progress && STAGE_AGENT[progress.stage] === key
      ? `${progress.stage[0].toUpperCase()}${progress.stage.slice(1)}${progress.total ? `: ${progress.done} of ${progress.total}` : '…'}` : null;
    const last = !mine.length && state.events.find((e) => e.agent === key && e.ms != null);
    const lastDetail = last && (last.phase === 'error' ? `Failed: ${last.detail}` : (last.detail || '').split('\n')[0]);
    return h('section', { class: 'agent', 'data-agent': key, 'data-busy': String(mine.length > 0), title: does },
      h('div', { class: 'agent-head' },
        h('h3', {}, h('span', { class: 'pulse', 'aria-hidden': 'true', hidden: !mine.length || null }), name, ' ',
          h('span', { class: 'role' }, role)),
        h('span', { class: 'state' }, mine.length ? `${mine.length} at work` : 'Idle')),
      h('p', { class: 'stats' }, stats, stage && h('span', { class: 'stage' }, ` · ${stage}`)),
      top.length > 0 && h('ol', { class: 'jobs' },
        top.slice(0, MAX_JOBS).flatMap((task) => jobRow(task, 0, childrenOf(task))),
        top.length > MAX_JOBS && h('li', { class: 'job more' }, `and ${top.length - MAX_JOBS} more`)),
      last && h('div', { class: 'last' },
        h('p', { class: 'last-head' }, `Last job · ${duration(last.ms)}`),
        h('p', { class: 'last-what' }, last.text),
        lastDetail && h('p', { class: 'last-detail' }, lastDetail)));
  }));
  return busy;
}

function renderActivity() {
  const busy = renderAgents();
  if (busy.length && !ticking) ticking = setInterval(renderAgents, 1000);
  if (!busy.length && ticking) {
    clearInterval(ticking);
    ticking = null;
  }
  if ($('#activity').hidden) return;
  $('#activity-empty').hidden = Boolean(state.id);
  const counts = { all: state.events.length };
  for (const e of state.events) counts[e.agent] = (counts[e.agent] || 0) + 1;
  $('#activity-filter').replaceChildren(...(state.id ? [['all', 'All'], ...AGENTS.map(([key, name]) => [key, name])] : [])
    .map(([key, label]) => h('button', {
      type: 'button', 'aria-pressed': String(state.filter === key), 'data-agent': key === 'all' ? null : key,
      onclick: () => { state.filter = key; renderActivity(); },
    }, `${label} ${counts[key] || 0}`)));
  // The latest steps; the whole log on request, and always in the export.
  const matching = state.events.filter((e) => state.filter === 'all' || e.agent === state.filter);
  const shown = state.timelineAll ? matching : matching.slice(0, TIMELINE_SHOWN);
  $('#timeline').replaceChildren(...(shown.length ? shown.map(timelineItem) : state.id ? [h('li', { class: 'muted' }, 'Nothing has happened yet.')] : []));
  $('#log-tools').hidden = !state.events.length;
  $('#log-count').textContent = `Full log: ${plural(state.events.length, 'step')}`;
  $('#timeline-all').hidden = matching.length <= TIMELINE_SHOWN;
  $('#timeline-all').textContent = state.timelineAll ? `Show the latest ${TIMELINE_SHOWN}` : `Show all ${matching.length}`;
}
const TIMELINE_SHOWN = 250;

// The whole agent activity log of the open search, oldest first, as CSV (a row per step) or JSON.
async function exportLog(format) {
  const id = state.id;
  const events = (await store.events(id)).reverse();
  const name = `agent-log-${id}`;
  if (format === 'csv') {
    download(`${name}.csv`, csvOf(LOG_COLUMNS, logRows(events, state.papers)), 'text/csv');
    return;
  }
  const titles = new Map(state.papers.map((p) => [p.id, p.title || p.ext_id]));
  const body = {
    search: { id, created_at: state.project.created_at, paragraph: state.project.paragraph },
    exported_at: new Date().toISOString(),
    agents: AGENT_NAMES,
    events: events.map(({ project_id: _, ...e }) => ({ ...e, paper_title: e.paper_title || titles.get(e.paper_id) || null })),
  };
  download(`${name}.json`, JSON.stringify(body, null, 2), 'application/json');
}

// One finished step: when, who, how long and how many tokens, what it was, and what came of it.
// A long outcome (an extraction's numbers, the search plan's queries) shows its first line and folds the rest.
function timelineItem(e) {
  const [first, ...rest] = (e.phase === 'error' ? `Failed: ${e.detail}` : e.detail || '').split('\n');
  return h('li', { 'data-agent': e.agent, 'data-phase': e.phase },
    h('span', { class: 'when' }, new Date(e.at).toLocaleTimeString()),
    h('span', { class: 'who' }, AGENT_NAMES[e.agent] || e.agent),
    e.ms != null && h('span', { class: 'when' }, ` · ${duration(e.ms)}`),
    e.usage && h('span', { class: 'when' }, ` · ${tokens(e.usage.input)} in · ${tokens(e.usage.output)} out`),
    h('span', { class: 'what' }, e.text),
    first && h('span', { class: 'detail' }, first),
    rest.length > 0 && h('details', { class: 'more' }, h('summary', {}, `${rest.length} more ${rest.length === 1 ? 'line' : 'lines'}`),
      h('span', { class: 'detail' }, rest.join('\n'))));
}

// -- step 1 ----------------------------------------------------------------------

function renderProblem(project) {
  const paragraph = $('#paragraph');
  paragraph.value = project.paragraph;
  paragraph.readOnly = true;
  $('#read').hidden = true;
  $('#delete-project').hidden = jobs.has(project.id);
  $('#read-status').textContent = project.status === 'reading' ? 'Opus is reading the problem…' : '';
  showError($('#problem-error'), !project.spec && project.error ? project.error : '');
  if (!project.spec && project.status === 'interrupted') {
    $('#read').hidden = false;
    $('#read').textContent = 'Read the problem again';
  }
}

async function readProblem() {
  const text = $('#paragraph').value.trim();
  if (state.project && !state.project.spec) {
    // Retrying a search whose reading failed or was cut short.
    if (!haveKey()) return;
    const id = state.project.id;
    startJob(id, () => pipeline(id).readSpec(id), moveOn(id, 'problem', 'spec'));
    return;
  }
  if (text.length < 20) {
    showError($('#problem-error'), 'Describe the study in a sentence or two first.');
    return;
  }
  if (!haveKey()) return;
  const id = await store.createProject(text);
  await store.updateProject(id, { status: 'reading' });
  startJob(id, () => pipeline(id).readSpec(id), moveOn(id, 'problem', 'spec'));
  await openProject(id);
}

// -- step 2 ----------------------------------------------------------------------

// The specification, as structured tables edited one part at a time; an edit is kept with the search.
const specPanel = new SpecPanel($('#spec-panel'), {
  onSave: async (spec) => {
    state.specKey = JSON.stringify(spec); // already shown: the redraw after saving leaves the panel as it is
    await store.updateProject(state.id, { spec });
  },
});

function renderSpec(project) {
  state.available.spec = Boolean(project.spec);
  if (!project.spec) return;
  const key = JSON.stringify(project.spec);
  if (key !== state.specKey) {
    state.specKey = key;
    specPanel.show(project.spec);
  }
  const running = jobs.has(project.id);
  // A new search replaces the papers a summary being written is about: it waits for the summary.
  const summarizing = jobs.has(`summary-${project.id}`);
  specPanel.disabled = running;
  $('#run').disabled = running || summarizing;
  const hasPapers = state.papers.length > 0;
  $('#run').textContent = hasPapers ? 'Search again' : 'Search and extract';
  $('#run-status').textContent = summarizing ? 'Wait until the summary is written.'
    : hasPapers && !running ? 'Searching again replaces the papers and decisions below.' : '';
}

async function runSearch(event) {
  event.preventDefault();
  if (!haveKey() || jobs.has(`summary-${state.id}`)) return;
  const id = state.id;
  const spec = specPanel.collect();
  if (!spec) return; // an open edit has a value to fix; the panel says which
  if (state.papers.length && !confirm('Search again? This replaces the papers and your decisions for this search.')) return;
  await store.updateProject(id, { spec, status: 'searching' });
  state.papersKey = '';
  startJob(id, () => pipeline(id).run(id), moveOn(id, 'progress', 'review'));
  state.available.progress = true;
  selectStep('progress');
  await refresh();
}

function continueSearch() {
  if (!haveKey() || jobs.has(state.id)) return;
  const id = state.id;
  startJob(id, () => pipeline(id).run(id, { resume: true }), moveOn(id, 'progress', 'review'));
  selectStep('progress');
  refresh();
}

// -- step 3 ----------------------------------------------------------------------

function renderProgress(project) {
  const counts = project.counts || {};
  const running = jobs.has(project.id);
  const started = RUNNING.has(project.status) && project.status !== 'reading';
  state.available.progress = Boolean(started || Object.keys(counts).length || project.search_plan || (project.spec && project.error));
  if (!state.available.progress) return;

  $('#map-body').replaceChildren(searchMap({ project, papers: state.papers, running, fetcher: true }));

  const progress = project.progress || {};
  const bar = $('#progress');
  bar.hidden = !running;
  bar.max = progress.total || 1;
  bar.value = progress.total ? progress.done : 0;

  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  const kept = total - (counts.screened_out || 0) - (counts.candidate || 0);
  const parts = [];
  if (running && progress.stage) parts.push(progress.total ? `${progress.stage}: ${progress.done} of ${progress.total}` : `${progress.stage}…`);
  if (total) parts.push(`${total} records found`, `${kept} kept after screening`, `${counts.extracted || 0} extracted`);
  $('#progress-text').textContent = parts.join(' · ');
  renderFunnel(project);
  $('#keep-open').hidden = !running;
  const stopped = !running && (project.status === 'interrupted' || (RUNNING.has(project.status) && project.status !== 'reading'));
  $('#continue').hidden = !(stopped || (project.status === 'error' && project.spec));
  showError($('#run-error'), project.spec && project.error ? project.error : '');

  const plan = project.search_plan;
  $('#plan-details').hidden = !plan;
  if (plan) {
    // Searches planned before the top-down levels had a flat list of PubMed queries.
    const levels = plan.levels
      ? [...plan.levels].sort((x, y) => LEVELS.indexOf(x.level) - LEVELS.indexOf(y.level))
      : (plan.pubmed_queries || []).map((query) => ({ level: null, query, why: '' }));
    $('#plan-summary').textContent = plan.summary;
    $('#plan-queries').replaceChildren(
      ...levels.map((l) => h('li', {}, h('strong', {}, l.level ? LEVEL_NAMES[l.level] : 'PubMed'), l.why && ` · ${l.why}`, h('code', {}, l.query))),
      ...plan.clinicaltrials_queries.map((q) => h('li', {}, h('strong', {}, LEVEL_NAMES.registry), h('code', {}, q))),
      ...(plan.extra_ids.length ? [h('li', {}, h('strong', {}, LEVEL_NAMES.named), h('code', {}, plan.extra_ids.join(', ')))] : []),
      ...(plan.failed_queries || []).map((q) => h('li', {}, badge('Failed', 'err'), ' ', q)),
    );
  }
  $('#usage-details').hidden = !state.usage.length;
  $('#usage').replaceChildren(...state.usage.map((u) => h('tr', {},
    h('td', {}, u.step), h('td', {}, u.model), h('td', { class: 'num' }, u.calls),
    h('td', { class: 'num' }, u.input_tokens.toLocaleString()), h('td', { class: 'num' }, u.output_tokens.toLocaleString()),
    h('td', { class: 'num' }, u.cache_read_input_tokens.toLocaleString()), h('td', { class: 'num' }, u.web_fetch_requests || ''))));
}

// The search, level by level: what each found, how much of it screening kept, and how much was extracted.
function renderFunnel(project) {
  const log = project.search_log || [];
  $('#funnel-wrap').hidden = !log.length;
  if (!log.length) return;
  const number = (n) => (n == null ? '' : Number(n).toLocaleString('en-US'));
  const counts = (level) => {
    const mine = state.papers.filter((p) => (p.found_in || []).includes(level));
    return [mine.length, mine.filter((p) => !['screened_out', 'candidate'].includes(p.status)).length, mine.filter((p) => p.status === 'extracted').length];
  };
  const searched = (e) => (e.error ? `Failed: ${e.error}`
    : `${number(e.hits)} ${e.hits === 1 ? 'hit' : 'hits'}; the top ${number(e.taken)} taken`);
  const how = (level, entries) => {
    if (level === 'cited') {
      const e = entries[0];
      if (!e.reviews) return 'No review passed screening, so there were no references to follow.';
      return `References of ${e.reviews} ${e.reviews === 1 ? 'review' : 'reviews'}: ${number(e.cited)} in PubMed, `
        + `${number(e.already)} already found, ${number(e.not_studies)} not studies${e.failed ? `; ${e.failed.length} could not be read` : ''}`;
    }
    if (level === 'named') return entries.map((e) => e.query).join(', ');
    return entries.map(searched).join(' · ');
  };
  const row = (level, label, mark) => {
    const entries = log.filter((e) => e.level === level);
    return h('tr', { 'data-level': level },
      h('td', { class: 'level' }, mark && h('span', { class: 'step', 'aria-hidden': 'true' }, mark), label, h('span', { class: 'how' }, how(level, entries))),
      ...counts(level).map((n) => h('td', { class: 'num' }, number(n))));
  };
  const rows = [h('tr', { class: 'group' }, h('th', { colspan: '4', scope: 'colgroup' }, 'Papers'))];
  let step = 0;
  for (const level of [...LEVELS, 'cited', 'named']) {
    if (!log.some((e) => e.level === level)) continue;
    const mark = LEVELS.includes(level) ? String(++step) : level === 'cited' ? '↳' : '';
    rows.push(row(level, LEVEL_NAMES[level], mark));
  }
  if (log.some((e) => e.level === 'registry')) {
    rows.push(h('tr', { class: 'group' }, h('th', { colspan: '4', scope: 'colgroup' }, LEVEL_NAMES.registry)));
    rows.push(row('registry', 'Studies with posted results', ''));
  }
  $('#funnel tbody').replaceChildren(...rows);
}

// -- step 4 ----------------------------------------------------------------------

function groupOf(paper) {
  const running = jobs.has(state.id);
  if (paper.status === 'extracted') return 'extracted';
  if (paper.status === 'screened_out') return 'excluded';
  if (['fetching', 'extracting'].includes(paper.status)) return 'pending';
  if (running && ['candidate', 'screened_in', 'unsure'].includes(paper.status)) return 'pending';
  return 'attention';
}

// The review shows one paper at a time: a progress line, a chip per paper to jump to, and the paper.
function renderReview() {
  const key = JSON.stringify([state.papers.map((p) => [p.id, p.status, p.extracted_by, p.error]), state.decisions, state.tab, state.source,
    state.reviewAt, state.project?.overlap, jobs.has(state.id)]);
  if (key === state.papersKey) return;
  state.papersKey = key;

  // Papers and registry records are reviewed separately.
  const bySource = Object.fromEntries(SOURCES.map(([source]) => [source, state.papers.filter((p) => kindOf(p) === source)]));
  if (!bySource[state.source].length) state.source = SOURCES.map(([source]) => source).find((source) => bySource[source].length) || 'papers';
  const extractedCount = (list) => list.filter((p) => p.status === 'extracted').length;
  $('#source-switch').replaceChildren(...SOURCES.map(([source, label]) => h('button', {
    type: 'button', 'aria-pressed': String(state.source === source),
    onclick: () => { state.source = source; state.papersKey = ''; renderReview(); },
  }, `${label} · ${extractedCount(bySource[source])} extracted of ${bySource[source].length}`)));
  $('#review-count').textContent = String(extractedCount(state.papers) || '');

  const groups = Object.fromEntries(TABS.map(([k]) => [k, []]));
  for (const paper of bySource[state.source]) groups[groupOf(paper)].push(paper);
  $('#tabs').replaceChildren(...TABS.map(([k, label]) => h('button', {
    type: 'button', class: 'secondary small', 'aria-pressed': String(state.tab === k),
    onclick: () => { state.tab = k; state.papersKey = ''; renderReview(); },
  }, `${label} (${groups[k].length})`)));

  const trials = new Map();
  for (const paper of state.papers) {
    for (const nct of paper.nct_ids || []) trials.set(nct, (trials.get(nct) || 0) + 1);
  }
  const shown = groups[state.tab];
  if (!shown.length) {
    $('#review-nav').hidden = true;
    state.cardKey = '';
    $('#papers').replaceChildren(h('p', { class: 'muted' }, 'Nothing here.'));
    return;
  }
  const at = `${state.source}:${state.tab}`;
  const current = reviewNav({
    papers: shown, current: state.reviewAt[at], decisionsOf: (p) => state.decisions[p.id] || {},
    show: showPaper, counting: state.tab === 'extracted',
  });
  // The paper shown stays shown when others join the list, and its card is redrawn only when it
  // changes, so what the reader has open stays open while the search runs.
  state.reviewAt[at] = current.id;
  const cardKey = JSON.stringify([current.id, current.status, current.extracted_by, current.error, current.fulltext_kind,
    state.decisions[current.id], state.project?.overlap?.report, jobs.has(state.id), jobs.has(`paper-${current.id}`),
    (current.nct_ids || []).map((nct) => trials.get(nct))]);
  if (cardKey !== state.cardKey) {
    state.cardKey = cardKey;
    $('#papers').replaceChildren(paperCard(current, trials));
  }
}

function showPaper(paperId) {
  state.reviewAt[`${state.source}:${state.tab}`] = paperId;
  renderReview();
  $('#papers .paper h3')?.scrollIntoView({ block: 'nearest' });
}

// -- step 6: the summary -----------------------------------------------------------------

function renderSummary(project) {
  const summary = project.summary;
  const running = summary?.status === 'running' || jobs.has(`summary-${project.id}`);
  $('#summarize').disabled = running || jobs.has(project.id);
  $('#summarize').textContent = summary?.status === 'done' ? 'Write the summary again' : 'Write the summary';
  $('#summarize-status').textContent = running ? 'Opus is checking for overlap and writing the summary…'
    : jobs.has(project.id) ? 'Wait until the search finishes.' : '';
  showError($('#summary-error'), summary?.status === 'error' ? summary.error
    : summary?.last_error ? `Writing the summary again failed: ${summary.last_error} This is the previous summary.` : '');
  const key = JSON.stringify([summary, running, project.overlap?.report, decisionsBasis(state.papers, state.decisions),
    state.papers.map((p) => [p.id, p.title, p.authors, p.year, p.pmid, p.pmcid])]);
  if (key === state.summaryKey) return;
  state.summaryKey = key;
  $('#summary-body').replaceChildren(...(summary?.status === 'done'
    ? summaryView({ summary, overlap: project.overlap?.report, papers: state.papers, decisions: state.decisions, linksOf: links })
    : running ? [] : [h('p', { class: 'muted' },
      'Review the papers first: the summary uses every result you included or left undecided, and none you excluded.')]));
}

function writeSummary() {
  if (!haveKey() || !state.id) return;
  const id = state.id;
  startJob(`summary-${id}`, () => pipeline(id).summarize(id));
  refresh();
}

function badge(text, kind = '') {
  return h('span', { class: `badge ${kind}`.trim() }, text);
}

function links(paper) {
  const out = {};
  if (paper.source === 'europepmc') out['Europe PMC'] = europepmcUrl(paper.ext_id);
  if (paper.pmid) out.PubMed = `https://pubmed.ncbi.nlm.nih.gov/${paper.pmid}/`;
  if (paper.pmcid) out.PMC = `https://pmc.ncbi.nlm.nih.gov/articles/${paper.pmcid}/`;
  if (paper.doi) out.DOI = `https://doi.org/${paper.doi}`;
  for (const nct of paper.nct_ids || []) out[nct] = `https://clinicaltrials.gov/study/${nct}`;
  return out;
}

// A paper for review, kept short: its title (linked to where it was found), key, authors and year,
// warnings only when there is something to watch, and each result as a line per group. Everything
// else (how it was found, the text used, which model extracted it and why) is under Details.
function paperCard(paper, trials) {
  const decisions = state.decisions[paper.id] || {};
  const paperDecision = decisions[PAPER]?.decision;
  const sharedTrial = (paper.nct_ids || []).find((nct) => trials.get(nct) > 1);
  const busy = jobs.has(state.id) || jobs.has(`paper-${paper.id}`);
  const extracted = paper.status === 'extracted' && paper.extraction;
  const overlap = overlapNote(paper, state.project?.overlap?.report, state.papers);
  const differs = extracted && !paper.extraction.matches_spec && paper.extraction.mismatch_reasons.length > 0;

  // Excluding the whole paper sits beside its title.
  const exclude = extracted && h('button', {
    type: 'button', class: 'secondary small exclude-paper', 'aria-pressed': String(paperDecision === 'exclude'),
    onclick: () => decide(paper, PAPER, paperDecision === 'exclude' ? null : 'exclude', ''),
  }, paperDecision === 'exclude' ? 'Undo excluding the paper' : 'Exclude the whole paper');
  const head = [
    ...paperHeader(paper, links(paper), {
      action: exclude,
      loadAuthors: (p) => new Pipeline({ store, claude: null, sources: literature() }).authorList(p.id),
      warnings: [
        paper.ext_id.startsWith('PPR') && badge('Preprint, not peer reviewed', 'warn'),
        paper.fulltext_kind === 'abstract' && badge('Abstract only', 'warn'),
        differs && badge('Differs from the plan', 'warn'),
        !overlap && sharedTrial && badge(`Another record shares ${sharedTrial}`, 'warn'),
      ],
    }),
    overlap,
  ];

  const screen = paper.screen;
  const reasons = paper.escalation?.reasons || [];
  const details = foldable(`details:${paper.id}`, { class: 'paper-details' }, 'Details',
    h('div', { class: 'meta' },
      badge({ clinicaltrials: 'ClinicalTrials.gov', pubmed: 'PubMed' }[paper.source] || 'Europe PMC'),
      paper.source !== 'clinicaltrials' && LEVEL_SHORT[paper.found_in?.[0]]
        && h('span', { class: 'badge', title: (paper.found_by || []).join('\n') }, LEVEL_SHORT[paper.found_in[0]]),
      paper.fulltext_kind && badge(TEXT_KIND[paper.fulltext_kind] || paper.fulltext_kind),
      paper.fetched_by === 'sonnet' && badge('Fetched by Sonnet'),
      paper.extracted_by === 'manager' && badge('Reviewed by Opus', 'accent'),
      paper.extracted_by === 'worker' && badge('Extracted by Haiku'),
      paper.escalation?.audit && badge('Audit sample')),
    extracted && screen && h('p', { class: 'reason' }, `Screening (${screen.decision}): ${screen.reason}`),
    reasons.length > 0 && h('p', { class: 'reason' }, `Why Opus reviewed it: ${reasons.join('; ')}`),
    differs && h('p', { class: 'reason' }, `Differs from the plan: ${paper.extraction.mismatch_reasons.join('; ')}`),
    extracted && paper.extraction.notes && h('p', { class: 'reason' }, `Notes: ${paper.extraction.notes}`),
    extracted && !busy && paper.source !== 'clinicaltrials' && h('div', { class: 'paper-actions' },
      h('button', { type: 'button', class: 'secondary small', onclick: () => chooseUpload(paper) }, 'Re-extract from your PDF')));

  // What else to do with it: read its text; get the text of one that has none yet.
  const actions = h('div', { class: 'paper-actions' });
  if (paper.fulltext_kind) {
    actions.append(h('button', { type: 'button', class: 'secondary small', onclick: () => showSource(paper) }, 'Show source text'));
  }
  if (!busy && ['screened_out', 'no_fulltext', 'needs_manual', 'screened_in', 'unsure', 'candidate', 'error'].includes(paper.status)) {
    actions.append(h('button', { type: 'button', class: 'secondary small', onclick: () => extractPaper(paper) },
      paper.status === 'screened_out' ? 'Extract anyway' : 'Try extracting again'));
  }
  if (!busy && !extracted && paper.source !== 'clinicaltrials') {
    actions.append(h('button', { type: 'button', class: 'secondary small', onclick: () => chooseUpload(paper) }, 'Upload the PDF'));
  }

  const body = [];
  if (extracted) {
    if (!paper.extraction.results.length) body.push(h('p', { class: 'reason muted' }, 'No relevant results were reported.'));
    paper.extraction.results.forEach((result, i) => body.push(resultBlock(paper, result, i, decisions)));
  } else {
    // Not extracted: why, which is what matters here.
    if (screen) body.push(h('p', { class: 'reason' }, `Screening (${screen.decision}): ${screen.reason}`));
    if (paper.error) body.push(h('p', { class: 'reason' }, badge('Note', 'warn'), ' ', paper.error));
  }
  return h('article', { class: 'paper' }, head, body, actions.childElementCount ? actions : null, details);
}

function resultBlock(paper, result, index, decisions) {
  return resultBox(paper, result, index, decisions, (i, decision, reason) => decide(paper, i, decision, reason));
}

async function decide(paper, resultIndex, decision, reason) {
  await store.setDecision(state.id, paper.id, resultIndex, decision, reason);
  await refreshNow(); // show the choice at once rather than with the next scheduled redraw
}

function extractPaper(paper, upload = null, uploadName = '') {
  if (!haveKey()) return;
  const projectId = state.id;
  const spec = state.project.spec;
  state.tab = 'pending';
  state.papersKey = '';
  startJob(`paper-${paper.id}`, async () => {
    await store.clearDecisions(paper.id);
    await pipeline(projectId).extractPaper(projectId, spec, paper.id, { upload, uploadName });
  });
  refresh();
}

function chooseUpload(paper) {
  if (!haveKey()) return;
  state.uploadFor = paper;
  $('#upload').value = '';
  $('#upload').click();
}

async function uploaded() {
  const file = $('#upload').files[0];
  const paper = state.uploadFor;
  if (!file || !paper) return;
  const bytes = new Uint8Array(await file.arrayBuffer());
  if (!(bytes[0] === 0x25 && bytes[1] === 0x50 && bytes[2] === 0x44 && bytes[3] === 0x46)) {
    alert('That file is not a PDF.');
    return;
  }
  extractPaper(paper, bytes, file.name);
}

// -- source text and exports ---------------------------------------------------------

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
  dialog.showModal();
  const doc = await store.document(paper.id);
  if (!doc) {
    pre.textContent = 'No text was retrieved for this paper.';
    return;
  }
  const text = doc.text || '';
  const quotes = (paper.extraction?.results || []).flatMap((r) => r.values.map((v) => v.quote));
  const nodes = [];
  let at = 0;
  for (const [start, end] of quoteRanges(text, quotes)) {
    nodes.push(text.slice(at, start), h('mark', {}, text.slice(start, end)));
    at = end;
  }
  nodes.push(text.slice(at));
  pre.replaceChildren(...nodes);
  if (doc.pdf) pre.prepend(h('span', { class: 'muted' }, 'Text layer of the PDF; tables may be out of order.\n\n'));
  pre.querySelector('mark')?.scrollIntoView({ block: 'center' });
}

function exportCsv(includedOnly) {
  const rows = exportRows(state.papers, state.decisions, includedOnly);
  download(`effect-sizes-${state.id}${includedOnly ? '-included' : ''}.csv`, toCsv(rows), 'text/csv');
}

async function exportJson() {
  const { id, created_at, paragraph, spec, search_plan, search_log, overlap, summary } = state.project;
  const papers = state.papers.map((p) => ({ ...p, decisions: state.decisions[p.id] || {}, links: links(p) }));
  const activity = (await store.events(id)).reverse().map(({ project_id: _, ...e }) => e);
  const body = { project: { id, created_at, paragraph, spec, search_plan, search_log, overlap, summary }, papers, usage: state.usage, activity };
  download(`effect-sizes-${id}.json`, JSON.stringify(body, null, 2), 'application/json');
}

// -- start -------------------------------------------------------------------------

async function start() {
  $('#read').addEventListener('click', readProblem);
  $('#spec-form').addEventListener('submit', runSearch);
  $('#new-project').addEventListener('click', newSearch);
  $('#delete-project').addEventListener('click', deleteProject);
  $('#continue').addEventListener('click', continueSearch);
  $('#source-close').addEventListener('click', () => $('#source-dialog').close());
  $('#api-key').addEventListener('input', saveKey);
  $('#remember-key').addEventListener('change', saveKey);
  $('#forget-key').addEventListener('click', forgetKey);
  $('#check-key').addEventListener('click', checkKey);
  $('#contact-email').addEventListener('change', (e) => writeSetting(EMAIL, e.target.value.trim()));
  $('#ncbi-key').addEventListener('change', (e) => writeSetting(NCBI_KEY, e.target.value.trim()));
  $('#upload').addEventListener('change', uploaded);
  $('#export-included').addEventListener('click', () => exportCsv(true));
  $('#export-all').addEventListener('click', () => exportCsv(false));
  $('#export-json').addEventListener('click', exportJson);
  $('#summarize').addEventListener('click', writeSummary);
  $('#export-log-csv').addEventListener('click', () => exportLog('csv'));
  $('#export-log-json').addEventListener('click', () => exportLog('json'));
  $('#timeline-all').addEventListener('click', () => { state.timelineAll = !state.timelineAll; renderActivity(); });
  document.addEventListener('keydown', reviewKeys);
  for (const step of STEPS) {
    $(`#tab-${step}`).addEventListener('click', () => selectStep(step));
    $(`#tab-${step}`).addEventListener('keydown', tabKeys);
  }
  $('#setup-done').addEventListener('click', () => {
    if (!haveKey()) return;
    selectStep(state.project ? defaultStep(state.project) : 'problem', true);
  });
  $('#roles').replaceChildren(...AGENTS.map(([key, name, role, does]) => h('li', { 'data-agent': key },
    h('strong', {}, `${name} · ${role}`), does)));
  $('#activity-toggle').addEventListener('click', () => setActivityOpen($('#activity').hidden));
  $('#activity-close').addEventListener('click', () => setActivityOpen(false));
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !$('#activity').hidden && window.innerWidth < 1280) setActivityOpen(false);
  });
  const remembered = readSetting(ACTIVITY_OPEN);
  // Below 1280px the panel covers the page, so there it opens only when asked.
  setActivityOpen(window.innerWidth >= 1280 && remembered !== 'closed', false);
  renderTabs();
  window.addEventListener('beforeunload', (event) => {
    if (jobs.size) event.preventDefault();
  });
  $('#models').textContent = `Opus ${MODELS.manager.replace('claude-opus-', '').replace('-', '.')} plans and reviews · `
    + `Sonnet ${MODELS.fetcher.replace('claude-sonnet-', '').replace('-', '.')} fetches · `
    + `Haiku ${MODELS.worker.replace('claude-haiku-', '').replace('-', '.')} screens and reads`;
  loadSettings();
  showKeyStatus();
  // Setup comes first until there is a key; after that, a new visit starts at Describe.
  state.step = apiKey() ? 'problem' : 'setup';
  renderTabs();
  await store.interruptRunning();
  await loadProjects();
  const id = location.hash.slice(1);
  if (id) await openProject(id);
}

start();
