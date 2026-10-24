// Effect Size Finder: the page. Talks to the JSON API in python/esf/server.py.

import { decisionsBasis } from '/lib/evidence.js';
import { foldable, overlapNote, paperHeader, resultBox, reviewKeys, reviewNav, summaryView } from '/lib/review-view.js';
import { keyKind, keyProblem, LEGACY_MODELS, MODEL_CHOICES, modelLabel, PROVIDERS, ROLE_INFO, roleNames } from '/lib/providers.js';
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
  provider: 'anthropic', models: {}, chatgpt: null, signInTimer: null, agentsOpen: false, scrollY: 0, namesKey: '',
  id: null, project: null, papers: [], tab: 'extracted', timer: null, specKey: '', papersKey: '', config: {},
  reviewAt: {}, // tab -> the paper id shown there
};
// How the agents run (see PROVIDERS in /lib/providers.js): 'anthropic' (Claude with an API key),
// 'openai' (OpenAI's models with an API key) or 'chatgpt' (OpenAI's models on the signed-in plan).
const PROVIDER = 'esf-provider';
const MODELS = (provider) => `esf-models-${provider}`; // a key provider's models for the roles: {manager, worker}

// The two API keys, each with its own box and storage, and sent only in its own vendor's header.
const KEYS = {
  anthropic: {
    storage: 'esf-anthropic-api-key', box: '#anthropic-box', input: '#api-key', remember: '#remember-key', forget: '#forget-key',
    status: '#key-status', header: 'X-Anthropic-Api-Key', serverHas: 'server_has_key', runs: 'Claude',
  },
  openai: {
    storage: 'esf-openai-api-key', box: '#openai-box', input: '#openai-key', remember: '#remember-openai-key', forget: '#forget-openai-key',
    status: '#openai-key-status', header: 'X-OpenAI-Api-Key', serverHas: 'server_has_openai_key', runs: "OpenAI's models",
  },
};
// What goes where, for the privacy notice on the Agents page.
const PRIVACY = {
  anthropic: "Your paragraph and the text of the papers go from this server to Anthropic's API, where Claude reads them, and so does your key if you enter one; the server does not store it. Don't paste anything you can't share with Anthropic.",
  openai: "Your paragraph and the text of the papers go from this server to OpenAI's API, where OpenAI's models read them, and so does your key if you enter one; the server does not store it. Don't paste anything you can't share with OpenAI.",
  chatgpt: "Your paragraph and the text of the papers go from this server to OpenAI's API, where OpenAI's models read them on your ChatGPT plan. The sign-in stays in this server's memory; only the app's registration and the chosen models are saved. Don't paste anything you can't share with OpenAI.",
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

// -- API keys --------------------------------------------------------------------
// Kept in this tab (sessionStorage) or, if asked, on this browser (localStorage). Each is sent only
// with the requests that start work on its own vendor's models.

function storage(kind) {
  try {
    return kind === 'local' ? window.localStorage : window.sessionStorage;
  } catch {
    return null;
  }
}

function loadKeys() {
  for (const { storage: name, input, remember } of Object.values(KEYS)) {
    const remembered = storage('local')?.getItem(name);
    $(input).value = remembered || storage('session')?.getItem(name) || '';
    $(remember).checked = Boolean(remembered);
  }
}

// A key that is surely another (keyProblem) is not kept for this provider: it stays in the box only.
function saveKey(provider) {
  const { storage: name, remember } = KEYS[provider];
  const key = keyOf(provider);
  try {
    storage('local')?.removeItem(name);
    storage('session')?.removeItem(name);
    if (key && !keyProblem(provider, key)?.wrong) storage($(remember).checked ? 'local' : 'session')?.setItem(name, key);
  } catch { /* storage unavailable: the key lives only in the field */ }
  showAgents();
}

function forgetKey(provider) {
  $(KEYS[provider].input).value = '';
  $(KEYS[provider].remember).checked = false;
  saveKey(provider);
  $(KEYS[provider].input).focus();
}

const keyOf = (provider) => (KEYS[provider] ? $(KEYS[provider].input).value.trim() : '');

// What is wrong with the key in a provider's box, if anything (see keyProblem in /lib/providers.js).
const keyProblemOf = (provider) => keyProblem(provider, keyOf(provider));

function showKeyStatus(provider) {
  const { box, status, remember, serverHas, runs } = KEYS[provider];
  const problem = keyProblemOf(provider);
  $(box).dataset.missing = String(Boolean(problem));
  if (problem) {
    $(status).textContent = problem.text;
  } else if (keyOf(provider)) {
    $(status).textContent = `${$(remember).checked ? 'Remembered on this browser' : 'Kept for this tab only'}. `
      + `Sent only with requests that run ${runs}; the server does not store it.`;
  } else if (state.config[serverHas]) {
    $(status).textContent = 'Optional: the server has its own key.';
  } else {
    const [href, name] = PROVIDERS[provider].console;
    $(status).replaceChildren('Needed to read the problem, search, and extract. Create one in the ',
      h('a', { href, target: '_blank', rel: 'noopener' }, name), '.');
  }
}

// Why a provider cannot run a job yet, or '' when it can.
function notReady(provider = state.provider) {
  if (provider === 'chatgpt') {
    const { status, remote } = state.chatgpt || {};
    if (remote) return 'Your ChatGPT plan can be used only in a browser on the computer that runs the server.';
    if (status === 'connected') return '';
    return status === 'reauth_required' ? 'Your ChatGPT sign-in has expired. Sign in with ChatGPT again first.' : 'Sign in with ChatGPT first.';
  }
  const problem = keyProblemOf(provider);
  if (problem?.wrong) return problem.text;
  return keyOf(provider) || state.config[KEYS[provider].serverHas] ? '' : `Enter your ${PROVIDERS[provider].keyName} first.`;
}

// A job needs a ready provider; without one, the Agents page opens and says what is missing.
function agentsReady() {
  const reason = notReady();
  if (!reason) return true;
  openAgents();
  const { box, status, input } = KEYS[state.provider] || { box: '#chatgpt-box', status: '#chatgpt-status', input: '#chatgpt-sign-in' };
  $(box).dataset.missing = 'true';
  $(status).textContent = reason;
  $(input).focus();
  return false;
}

// -- the models: each role's, for the chosen provider ------------------------------------------

const defaultModels = (provider) => (provider === 'openai'
  ? { manager: state.config.openai_manager_model, worker: state.config.openai_worker_model }
  : { manager: state.config.manager_model, worker: state.config.worker_model });

// A key provider's models for a role, best first, with the server's default if it is another.
function modelChoices(provider, role) {
  const listed = MODEL_CHOICES[provider] || [];
  const fallback = defaultModels(provider)[role];
  return fallback && !listed.includes(fallback) ? [fallback, ...listed] : listed;
}

function loadModels() {
  for (const provider of Object.keys(KEYS)) {
    try {
      const saved = JSON.parse(storage('local')?.getItem(MODELS(provider)) || 'null');
      if (saved && typeof saved === 'object') state.models[provider] = saved;
    } catch { /* unreadable: the defaults */ }
  }
}

// The models a provider's roles run on: a key provider's as chosen on this browser, else the
// server's defaults; on a ChatGPT plan, the ones chosen for the sign-in (kept by the server).
function chosenModels(provider = state.provider) {
  if (provider === 'chatgpt') {
    const plan = state.chatgpt || {};
    return { manager: plan.manager_model || state.config.openai_manager_model, worker: plan.worker_model || state.config.openai_worker_model };
  }
  const saved = state.models[provider] || {};
  const pick = (role) => (modelChoices(provider, role).includes(saved[role]) ? saved[role] : defaultModels(provider)[role]);
  return { manager: pick('manager'), worker: pick('worker') };
}

// A model's name as people know it: the plan's own name for its models.
const planName = (slug) => state.chatgpt?.models?.find((m) => m.slug === slug)?.display_name || modelLabel(slug);
const modelName = (provider, model) => (provider === 'chatgpt' ? planName(model) : modelLabel(model));

// The names of the roles (see /lib/providers.js) for the chosen provider's models.
function names() {
  const models = chosenModels();
  if (state.provider !== 'chatgpt') return roleNames(models);
  return { ...roleNames({}), manager: planName(models.manager), worker: planName(models.worker) };
}

// The roles' names for a search: the models that served each role in it (the usage since it last
// started, and reading the problem), so past work is credited to the models that did it; the chosen
// ones for roles it has not used yet.
const STEP_ROLE = { spec: 'manager', search: 'manager', adjudicate: 'manager', overlap: 'manager', summarize: 'manager', screen: 'worker', extract: 'worker' };
const runUsage = (project) => project?.run_usage || project?.usage || [];
function searchNames(project) {
  const n = { ...names() };
  const seen = {};
  for (const u of runUsage(project)) {
    const role = STEP_ROLE[u.step];
    if (role && u.model) (seen[role] ||= new Set()).add(planName(u.model));
  }
  for (const [role, labels] of Object.entries(seen)) n[role] = [...labels].join(', ');
  return n;
}

// The model that did a step of the open search (the last that served it), for the lines about past
// work: who read the problem, who wrote the summary. The chosen manager when no model has yet.
function didBy(step) {
  const rows = runUsage(state.project).filter((u) => u.step === step && u.model);
  return rows.length ? planName(rows.at(-1).model) : names().manager;
}

function showDoers() {
  for (const el of document.querySelectorAll('[data-did]')) el.textContent = didBy(el.dataset.did);
}

// The roles' names for work this page started on the server: the models it started with, whatever is
// chosen since (after a reload, the ones chosen now).
const startedWith = {};
const jobNames = (key) => startedWith[key] || names();

async function chooseModels() {
  const manager = $('#model-manager').value;
  const worker = $('#model-worker').value;
  let failed = '';
  if (state.provider === 'chatgpt') {
    try {
      state.chatgpt = await api('/api/chatgpt/models', { method: 'PUT', body: { manager_model: manager, worker_model: worker } });
    } catch (error) {
      failed = error.message;
    }
  } else {
    state.models[state.provider] = { manager, worker };
    try {
      storage('local')?.setItem(MODELS(state.provider), JSON.stringify({ manager, worker }));
    } catch { /* storage unavailable: the choice lasts until the page reloads */ }
  }
  showAgents();
  if (failed) $('#chatgpt-status').textContent = failed;
}

// -- the Agents page -------------------------------------------------------------------

function openAgents() {
  if (!state.agentsOpen) state.scrollY = window.scrollY;
  state.agentsOpen = true;
  showPage();
  window.scrollTo(0, 0);
  $('#agents-title').focus({ preventScroll: true });
}

function closeAgents() {
  state.agentsOpen = false;
  showPage();
  window.scrollTo(0, state.scrollY);
  $('#agents-toggle').focus({ preventScroll: true });
}

// The Agents page is shown instead of the intro and the searches.
function showPage() {
  $('#agents-page').hidden = !state.agentsOpen;
  $('#intro').hidden = state.agentsOpen;
  $('#searches').hidden = state.agentsOpen;
  $('#agents-toggle').setAttribute('aria-expanded', String(state.agentsOpen));
}

// A key pasted in the other key provider's box (keyProblem tells the person to choose it) moves with them.
function chooseProvider(name) {
  const previous = state.provider;
  state.provider = PROVIDERS[name] ? name : 'anthropic';
  if (KEYS[previous] && KEYS[state.provider] && keyKind(keyOf(previous)) === state.provider && !keyOf(state.provider)) {
    $(KEYS[state.provider].input).value = keyOf(previous);
    $(KEYS[state.provider].remember).checked = $(KEYS[previous].remember).checked;
    $(KEYS[previous].input).value = '';
    $(KEYS[previous].remember).checked = false;
    saveKey(previous);
    saveKey(state.provider);
  }
  try { storage('local')?.setItem(PROVIDER, state.provider); } catch { /* storage unavailable */ }
  showAgents();
  if (state.provider === 'chatgpt' && state.chatgpt?.status === 'connected') loadChatGPT(true);
}

// Everything on the page that depends on the provider, its key or sign-in, and its models.
function showAgents() {
  const chosen = PROVIDERS[state.provider];
  for (const card of document.querySelectorAll('.provider-card')) {
    const input = card.querySelector('input');
    const provider = PROVIDERS[input.value];
    const models = chosenModels(input.value);
    input.checked = input.value === state.provider;
    card.querySelector('.provider-label').textContent = provider.label;
    card.querySelector('.provider-billing').textContent = provider.billing;
    card.querySelector('.provider-models').replaceChildren(...(models.manager ? [
      h('span', {}, `Manager ${modelName(input.value, models.manager)}`), ' · ', h('span', {}, `Workers ${modelName(input.value, models.worker)}`),
    ] : []));
    card.querySelector('.provider-ready').hidden = Boolean(notReady(input.value));
  }
  for (const name of ['anthropic', 'openai', 'chatgpt']) $(`#${name}-box`).hidden = name !== state.provider;
  for (const provider of Object.keys(KEYS)) showKeyStatus(provider);
  showChatGPT();
  showRoles();
  $('#agents-privacy').textContent = PRIVACY[state.provider];

  const reason = notReady();
  const toggle = $('#agents-toggle');
  toggle.textContent = `Agents: ${chosen.label}`;
  toggle.dataset.missing = String(Boolean(reason));
  toggle.title = reason || 'How the agents run';

  const n = names();
  for (const el of document.querySelectorAll('[data-name]')) {
    el.textContent = el.dataset.name === 'vendor' ? chosen.vendor : n[el.dataset.name] || el.textContent;
  }
  showDoers();
  if (state.config.manager_model) {
    $('#models').textContent = `${n.manager} plans and reviews · ${n.worker} screens and reads`
      + (state.config.contact_email_set ? '' : ' · set ESF_CONTACT_EMAIL to fetch open-access PDFs');
  }
  // The open search names the models too: redrawn when they change.
  const key = JSON.stringify([state.provider, n]);
  if (key !== state.namesKey) {
    state.namesKey = key;
    if (state.project) {
      renderProblem(state.project);
      renderProgress(state.project);
      if (state.papers.length) {
        state.papersKey = '';
        state.cardKey = '';
        renderReview();
      }
      renderSummary(state.project);
    }
  }
}

// Who does what: a row per role, with the models of the manager and the workers to choose from.
function showRoles() {
  const plan = state.provider === 'chatgpt';
  const signedIn = state.chatgpt?.status === 'connected' && !state.chatgpt.remote;
  const models = chosenModels();
  for (const row of document.querySelectorAll('.role')) {
    const [name, does] = ROLE_INFO[row.dataset.role];
    row.querySelector('.role-name').textContent = name;
    row.querySelector('.role-does').textContent = does;
  }
  for (const role of ['manager', 'worker']) {
    const select = $(`#model-${role}`);
    const chosen = models[role];
    let options;
    if (plan) {
      // A choice the plan doesn't list stays until the person picks another: the server keeps it as it is.
      const listed = (state.chatgpt?.models || []).map((m) => [m.slug, m.display_name]);
      options = !chosen || listed.some(([slug]) => slug === chosen) ? listed
        : [[chosen, listed.length ? `${planName(chosen)} (not on your plan)` : planName(chosen)], ...listed];
    } else {
      options = modelChoices(state.provider, role).map((m) => [m, modelLabel(m)]);
    }
    // Redrawn only when the list changes, so an open list is not closed under the reader.
    const key = JSON.stringify(options);
    if (select.dataset.options !== key) {
      select.dataset.options = key;
      select.replaceChildren(...options.map(([value, label]) => h('option', { value }, label)));
    }
    select.value = chosen || '';
    select.disabled = plan && !(signedIn && state.chatgpt.models?.length);
  }
  $('#roles-note').textContent = !plan ? 'Your choice is kept on this browser and used from the next step you start.'
    : signedIn ? 'The models your ChatGPT plan offers. The choice is kept on this server.'
      : "Sign in with ChatGPT to choose from your plan's models.";
}

// -- Sign in with ChatGPT: OpenAI's models on the person's plan ------------------------------

// The sign-in as the server has it: who, the plan's models and the chosen ones. Never a token.
async function loadChatGPT(refreshModels = false) {
  try {
    state.chatgpt = await api(`/api/chatgpt${refreshModels ? '?refresh=true' : ''}`);
  } catch (error) {
    state.chatgpt = { status: 'disconnected', error: error.message, models: [] };
  }
  showAgents();
  if (!state.chatgpt.pending) stopWaiting();
}

function stopWaiting() {
  clearInterval(state.signInTimer);
  state.signInTimer = null;
}

function showChatGPT() {
  const plan = state.chatgpt || { status: 'disconnected', models: [] };
  const box = $('#chatgpt-box');
  box.dataset.missing = 'false';
  const connected = plan.status === 'connected' && !plan.remote;
  const pending = Boolean(plan.pending) && !plan.remote;
  $('#chatgpt-sign-in').hidden = connected;
  $('#chatgpt-sign-in').textContent = plan.status === 'reauth_required' ? 'Sign in with ChatGPT again' : 'Sign in with ChatGPT';
  $('#chatgpt-sign-in').disabled = Boolean(plan.remote);
  $('#chatgpt-cancel').hidden = !pending;
  $('#chatgpt-sign-out').hidden = !connected;
  const who = [plan.name, plan.email && `(${plan.email})`].filter(Boolean).join(' ');
  $('#chatgpt-status').textContent = plan.remote
    ? 'Sign in with ChatGPT works only in a browser on the computer that runs the server: ChatGPT sends you back to 127.0.0.1.'
    : pending && !connected ? 'Finish signing in on the ChatGPT tab; this page notices when you are done.'
      : {
        connected: `Signed in${who ? ` as ${who}` : ''}. The models run on your ChatGPT plan; set this app's share of it in ChatGPT Settings. The sign-in lasts until the server stops.`,
        reauth_required: 'Your ChatGPT sign-in has expired or was revoked. Sign in again.',
        disconnected: 'Opens ChatGPT in a new tab. A ChatGPT Plus or Pro plan then runs the models, with no API key; only this server holds the sign-in, in memory.',
      }[plan.status] || '';
  if (pending && connected) $('#chatgpt-status').textContent += ' Another sign-in is waiting on a ChatGPT tab.';
  if (plan.error && !plan.remote) $('#chatgpt-status').textContent += ` ${plan.error}`;
}

async function signInWithChatGPT() {
  // Opened now, while the click still counts, so the browser doesn't block it; pointed at ChatGPT below.
  const tab = window.open('about:blank', '_blank');
  try {
    const { url } = await api('/api/chatgpt/sign-in', { method: 'POST' });
    if (tab) {
      tab.opener = null;
      tab.location.href = url;
    } else {
      window.location.href = url;
    }
  } catch (error) {
    tab?.close();
    $('#chatgpt-status').textContent = error.message;
    return;
  }
  await loadChatGPT();
  stopWaiting();
  state.signInTimer = setInterval(() => loadChatGPT(), 1500);
}

// Sign out, or give up a sign-in still waiting on the ChatGPT tab.
async function leaveChatGPT(action) {
  stopWaiting();
  let failed = '';
  try {
    state.chatgpt = await api(`/api/chatgpt/${action}`, { method: 'POST' });
  } catch (error) {
    failed = error.message;
  }
  showAgents();
  if (failed) $('#chatgpt-status').textContent = failed;
}

// What a job runs on, for the server: the provider; for a key provider its key, only in its own
// vendor's header, and the models chosen for the roles. Nothing else carries a key.
function agentHeaders() {
  const headers = { 'X-Model-Provider': state.provider };
  const key = KEYS[state.provider];
  if (!key) return headers;
  if (keyOf(state.provider) && !keyProblemOf(state.provider)?.wrong) headers[key.header] = keyOf(state.provider);
  const models = chosenModels();
  if (models.manager) headers['X-Manager-Model'] = models.manager;
  if (models.worker) headers['X-Worker-Model'] = models.worker;
  return headers;
}

async function api(path, options = {}) {
  const { withAgents, ...rest } = options;
  const init = { ...rest, headers: { 'Content-Type': 'application/json', ...(withAgents ? agentHeaders() : {}) } };
  if (init.body !== undefined && typeof init.body !== 'string') init.body = JSON.stringify(init.body);
  const response = await fetch(path, init);
  if (!response.ok && withAgents && state.provider === 'chatgpt') loadChatGPT();
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
  Object.assign(state, { id: null, project: null, papers: [], specKey: '', papersKey: '', summaryKey: '', cardKey: '', reviewAt: {}, busy: false });
  history.replaceState(null, '', location.pathname);
  const paragraph = $('#paragraph');
  paragraph.value = '';
  paragraph.readOnly = false;
  $('#read').hidden = false;
  $('#read-status').textContent = '';
  showError($('#problem-error'), '');
  for (const id of ['#spec', '#progress-card', '#review', '#summary']) $(id).hidden = true;
  loadProjects();
  paragraph.focus();
}

async function openProject(id) {
  clearTimeout(state.timer);
  Object.assign(state, { id, project: null, papers: [], specKey: '', papersKey: '', summaryKey: '', cardKey: '', reviewAt: {}, busy: false });
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
  state.papers = hasPapers ? await api(`/api/projects/${id}/papers`) : [];
  if (id !== state.id) return;
  renderProblem(project);
  showDoers();
  renderSpec(project);
  renderProgress(project);
  if (hasPapers) renderReview();
  else $('#review').hidden = true;
  renderSummary(project);
  const extracting = state.papers.some((p) => p.status === 'fetching' || p.status === 'extracting');
  const busy = RUNNING.has(project.status) || extracting || project.summary?.status === 'running';
  if (busy) {
    clearTimeout(state.timer);
    state.timer = setTimeout(() => refresh().catch(() => {}), 2500);
  } else if (wasRunning) {
    loadProjects();
  }
  // A job on the plan has ended: the sign-in may have expired or hit the plan's limit meanwhile.
  if (state.busy && !busy && state.provider === 'chatgpt') loadChatGPT();
  state.busy = busy;
}

// -- step 1 ----------------------------------------------------------------------

function renderProblem(project) {
  const paragraph = $('#paragraph');
  paragraph.value = project.paragraph;
  paragraph.readOnly = true;
  $('#read').hidden = true;
  $('#read-status').textContent = project.status === 'reading' ? `${jobNames(project.id).manager} is reading the problem…` : '';
  showError($('#problem-error'), !project.spec && project.error ? project.error : '');
}

async function readProblem() {
  const text = $('#paragraph').value.trim();
  if (text.length < 20) {
    showError($('#problem-error'), 'Describe the study in a sentence or two first.');
    return;
  }
  if (!agentsReady()) return;
  $('#read').disabled = true;
  try {
    const started = names();
    const { id } = await api('/api/projects', { method: 'POST', body: { paragraph: text }, withAgents: true });
    startedWith[id] = started;
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
    specPanel.show(project.spec, { replace: state.specShownFor !== project.id });
    state.specShownFor = project.id;
  }
  const running = RUNNING.has(project.status);
  // A new search replaces the papers a summary being written is about: it waits for the summary.
  const summarizing = project.summary?.status === 'running';
  specPanel.disabled = running;
  $('#run').disabled = running || summarizing;
  const hasPapers = Object.keys(project.counts || {}).length > 0;
  $('#run').textContent = hasPapers ? 'Search again' : 'Search and extract';
  $('#run-status').textContent = summarizing ? 'Wait until the summary is written.'
    : hasPapers && !running ? 'Searching again replaces the papers and decisions below.' : '';
}

async function runSearch(event) {
  event.preventDefault();
  const hasPapers = Object.keys(state.project.counts || {}).length > 0;
  if (!agentsReady() || state.project.summary?.status === 'running') return;
  const spec = specPanel.collect();
  if (!spec) return; // an open edit has a value to fix; the panel says which
  if (hasPapers && !confirm('Search again? This replaces the papers and your decisions for this search.')) return;
  $('#run').disabled = true;
  try {
    await api(`/api/projects/${state.id}/spec`, { method: 'PUT', body: spec });
    await api(`/api/projects/${state.id}/run`, { method: 'POST', withAgents: true });
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

  $('#map-body').replaceChildren(searchMap({ project, papers: state.papers, running: RUNNING.has(project.status), fetcher: false, names: searchNames(project) }));

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
      if (e.error) return ` (failed: ${e.error})`;
      return e.added == null ? ` (${count(e.hits, 'hit')})` : ` (${count(e.hits, 'hit')}; ${count(e.added, 'new record')} screened)`;
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
  if (!current) {
    state.cardKey = '';
    $('#papers').replaceChildren(h('p', { class: 'muted' }, 'Nothing here.'));
    return;
  }
  // The paper shown stays shown when others join the list, and its card is redrawn only when it
  // changes, so what the reader has open (an author table, quotes) stays open while the search runs.
  state.reviewAt[state.tab] = current.id;
  const cardKey = JSON.stringify([current.id, current.status, current.extracted_by, current.error, current.fulltext_kind,
    current.decisions, state.project.overlap?.report, RUNNING.has(state.project.status),
    (current.nct_ids || []).map((nct) => trials.get(nct))]);
  if (cardKey !== state.cardKey) {
    state.cardKey = cardKey;
    $('#papers').replaceChildren(paperCard(current, trials));
  }
}

function showPaper(paperId) {
  state.reviewAt[state.tab] = paperId;
  renderReview();
  $('#papers .paper h3')?.scrollIntoView({ block: 'nearest' });
}

// The model that made a paper's extraction. One saved without it was made before OpenAI's models were
// offered, so by Claude's (the defaults then), whichever provider is chosen now.
const extractor = (paper) => modelLabel(paper.extracted_model || LEGACY_MODELS[paper.extracted_by] || paper.extracted_by);

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
  const overlap = overlapNote(paper, state.project.overlap?.report, state.papers);
  const differs = extracted && !paper.extraction.matches_spec && paper.extraction.mismatch_reasons.length > 0;

  // Excluding the whole paper sits beside its title.
  const exclude = extracted && h('button', {
    type: 'button', class: 'secondary small exclude-paper', 'aria-pressed': String(paperDecision === 'exclude'),
    onclick: () => decide(paper, -1, paperDecision === 'exclude' ? null : 'exclude', ''),
  }, paperDecision === 'exclude' ? 'Undo excluding the paper' : 'Exclude the whole paper');
  const head = [
    ...paperHeader(paper, paper.links || {}, {
      action: exclude,
      loadAuthors: async (p) => (await api(`/api/projects/${state.id}/papers/${p.id}/authors`)).authors,
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
      paper.fulltext_kind && badge(TEXT_KIND[paper.fulltext_kind] || paper.fulltext_kind),
      paper.extracted_by === 'manager' && badge(`Reviewed by ${extractor(paper)}`, 'accent'),
      paper.extracted_by === 'worker' && badge(`Extracted by ${extractor(paper)}`),
      paper.escalation?.audit && badge('Audit sample')),
    extracted && screen && h('p', { class: 'reason' }, `Screening (${screen.decision}): ${screen.reason}`),
    reasons.length > 0 && h('p', { class: 'reason' }, `Flagged for review: ${reasons.join('; ')}`),
    differs && h('p', { class: 'reason' }, `Differs from the plan: ${paper.extraction.mismatch_reasons.join('; ')}`),
    extracted && paper.extraction.notes && h('p', { class: 'reason' }, `Notes: ${paper.extraction.notes}`));

  // What else to do with it: read its text; get the text of one that has none yet.
  const actions = h('div', { class: 'paper-actions' });
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
  if (!agentsReady()) return;
  try {
    await api(`/api/projects/${state.id}/papers/${paper.id}/extract`, { method: 'POST', withAgents: true });
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
  $('#summarize-status').textContent = running ? `${jobNames(`summary-${project.id}`).manager} is checking for overlap and writing the summary…`
    : searching ? 'Wait until the search finishes.' : '';
  showError($('#summary-error'), summary?.status === 'error' ? summary.error
    : summary?.last_error ? `Writing the summary again failed: ${summary.last_error} This is the previous summary.` : '');
  const decisions = Object.fromEntries(state.papers.map((p) => [p.id, p.decisions || {}]));
  const key = JSON.stringify([summary, project.overlap?.report, decisionsBasis(state.papers, decisions),
    state.papers.map((p) => [p.id, p.title, p.authors, p.year, p.pmid, p.pmcid])]);
  if (key === state.summaryKey) return;
  state.summaryKey = key;
  $('#summary-body').replaceChildren(...(summary?.status === 'done'
    ? summaryView({ summary, overlap: project.overlap?.report, papers: state.papers, decisions, linksOf: (p) => p.links })
    : running ? [] : [h('p', { class: 'muted' },
      'Review the papers first: the summary uses every result you included or left undecided, and none you excluded.')]));
}

async function writeSummary() {
  if (!state.id || !agentsReady()) return;
  showError($('#summary-error'), '');
  $('#summarize').disabled = true;
  try {
    startedWith[`summary-${state.id}`] = names();
    await api(`/api/projects/${state.id}/summary`, { method: 'POST', withAgents: true });
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
  $('#summarize').addEventListener('click', writeSummary);
  // The review's arrow keys, only while the review is on screen.
  document.addEventListener('keydown', (event) => { if (!state.agentsOpen) reviewKeys(event); });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && state.agentsOpen && !document.querySelector('dialog[open]')) closeAgents();
  });
  $('#agents-toggle').addEventListener('click', () => (state.agentsOpen ? closeAgents() : openAgents()));
  $('#agents-back').addEventListener('click', closeAgents);
  $('#agents-done').addEventListener('click', closeAgents);
  for (const input of document.querySelectorAll('input[name="provider"]')) {
    input.addEventListener('change', () => chooseProvider(input.value));
  }
  for (const [provider, { input, remember, forget }] of Object.entries(KEYS)) {
    $(input).addEventListener('input', () => saveKey(provider));
    $(remember).addEventListener('change', () => saveKey(provider));
    $(forget).addEventListener('click', () => forgetKey(provider));
  }
  $('#model-manager').addEventListener('change', chooseModels);
  $('#model-worker').addEventListener('change', chooseModels);
  $('#chatgpt-sign-in').addEventListener('click', signInWithChatGPT);
  $('#chatgpt-cancel').addEventListener('click', () => leaveChatGPT('cancel'));
  $('#chatgpt-sign-out').addEventListener('click', () => leaveChatGPT('sign-out'));
  // Back from the ChatGPT tab: the sign-in may have finished there.
  window.addEventListener('focus', () => { if (state.provider === 'chatgpt') loadChatGPT(); });
  loadKeys();
  loadModels();
  try {
    const saved = storage('local')?.getItem(PROVIDER);
    if (PROVIDERS[saved]) state.provider = saved;
  } catch { /* storage unavailable */ }
  try {
    const config = await api('/api/config');
    state.config = config;
  } catch {
    $('#models').textContent = 'The server is not responding.';
  }
  showAgents();
  await loadChatGPT();
  // The page opens on the Agents page until the chosen provider can run the agents; a link to a search
  // opens the search (the Agents button says what is missing).
  if (notReady() && !location.hash.slice(1)) openAgents();
  // The plan's models, asked of OpenAI, need not hold up the page.
  if (state.provider === 'chatgpt' && state.chatgpt.status === 'connected') loadChatGPT(true);
  await loadProjects();
  const id = location.hash.slice(1);
  if (id) await openProject(id);
}

start();
