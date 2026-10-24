// This page is served by the local helper, not by GitHub Pages. The person must approve its opener
// before any account information or model results are sent. Every RPC is bound to that window,
// its exact origin and this connection's nonce. Only the fixed operations below are exposed.
const PROTOCOL = 'esf-chatgpt-v1';
const $ = (selector) => document.querySelector(selector);
const params = new URLSearchParams(location.hash.slice(1));
const channel = params.get('channel');
const origin = params.get('origin');
const parent = window.opener;
history.replaceState(null, '', location.pathname);
let allowed = false;
let generation = 0;
let state = null;
let accessKey = '';
let poll;
const inFlight = new Set();

function validOrigin(value) {
  try {
    const url = new URL(value);
    return value === url.origin && (url.protocol === 'https:'
      || (url.protocol === 'http:' && ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname)));
  } catch { return false; }
}

function error(message = '') {
  $('#error').textContent = message;
  $('#error').hidden = !message;
}

function refreshFailed(exc) {
  error(exc.message);
  if (allowed) send({ kind: 'state', state: { status: 'disconnected', models: [], error: exc.message } });
}

function send(data) {
  parent?.postMessage({ protocol: PROTOCOL, channel, ...data }, origin);
}

async function api(path, body) {
  // The one-time installer/helper open can pair another tab while this companion is already open.
  try { accessKey = localStorage.getItem('esf-access-key') || ''; } catch { /* use the key already read */ }
  const response = await fetch(path, {
    method: body === undefined ? 'GET' : 'POST',
    headers: { 'Content-Type': 'application/json', 'X-ESF-Token': accessKey },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    cache: 'no-store',
  });
  const result = await response.json();
  if (!response.ok) {
    const detail = result.error || {};
    const message = result.code === 'access_key'
      ? 'Pair this browser with esf helper open, then connect again. For a manually started helper, open the address printed by esf serve.'
      : detail.message || result.detail || 'The local helper could not complete this request.';
    throw Object.assign(new Error(message),
      { status: response.status, code: detail.code || result.code || 'bridge_error', retryable: Boolean(detail.retryable) });
  }
  return result;
}

async function refresh(models = false) {
  const mine = generation;
  const next = await api(`/api/chatgpt${models ? '?refresh=true' : ''}`);
  if (mine !== generation) return;
  if (next.remote) throw new Error('Open the helper on the same computer as this browser, at http://127.0.0.1.');
  state = next;
  const connected = state.status === 'connected';
  const who = [state.name, state.email].filter(Boolean).join(' · ');
  $('#status').textContent = connected ? `Signed in${who ? ` as ${who}` : ''}. ${state.models.length} models available.`
    : state.pending ? 'Finish signing in on the ChatGPT tab.' : 'Sign in to let the website use your ChatGPT plan.';
  $('#sign-in').hidden = connected && !state.error && !state.reconsent;
  $('#cancel').hidden = !state.pending;
  $('#another').hidden = !state.registered;
  $('#refresh').hidden = $('#sign-out').hidden = !connected;
  error(state.error || '');
  if (allowed) send({ kind: 'state', state });
}

function disconnect() {
  allowed = false;
  generation++;
  clearInterval(poll);
  send({ kind: 'closed' });
  $('#account').hidden = $('#approval').hidden = true;
  $('#consent').textContent = 'The website is disconnected. You can close this window.';
}

async function signIn(another = false) {
  // Open during the click, before awaiting the helper, so popup blockers do not swallow sign-in.
  const tab = window.open('about:blank', '_blank');
  if (!tab) return error('Allow this window to open the ChatGPT sign-in tab, then try again.');
  tab.opener = null;
  try {
    const { url } = await api(`/api/chatgpt/${another ? 'another-account' : 'sign-in'}`, {});
    tab.location.href = url;
    await refresh();
  } catch (exc) {
    tab.close();
    error(exc.message);
  }
}

window.addEventListener('message', async (event) => {
  const data = event.data;
  if (!allowed || event.source !== parent || event.origin !== origin || data?.protocol !== PROTOCOL
      || data.channel !== channel || data.kind !== 'request' || typeof data.id !== 'string'
      || data.id.length > 100 || inFlight.has(data.id)) return;
  if (!['responses', 'state'].includes(data.method)) return;
  const mine = generation;
  inFlight.add(data.id);
  try {
    const result = data.method === 'responses' ? await api('/api/chatgpt/responses', data.body)
      : await api('/api/chatgpt?refresh=true');
    if (allowed && mine === generation) send({ kind: 'reply', id: data.id, result });
  } catch (exc) {
    if (allowed && mine === generation) {
      send({ kind: 'reply', id: data.id, error: { code: exc.code || 'bridge_error', message: exc.message,
        status: exc.status || 502, retryable: Boolean(exc.retryable) } });
      await refresh().catch(refreshFailed);
    }
  } finally {
    inFlight.delete(data.id);
  }
});

$('#allow').addEventListener('click', async () => {
  allowed = true;
  $('#approval').hidden = true;
  $('#account').hidden = false;
  try { await refresh(true); } catch (exc) { refreshFailed(exc); }
  poll = setInterval(() => {
    if (parent.closed) disconnect();
    else refresh().catch(refreshFailed);
  }, 2000);
});
$('#decline').addEventListener('click', disconnect);
$('#disconnect').addEventListener('click', disconnect);
$('#sign-in').addEventListener('click', () => signIn());
$('#cancel').addEventListener('click', async () => {
  try { await api('/api/chatgpt/cancel', {}); await refresh(); } catch (exc) { error(exc.message); }
});
$('#another').addEventListener('click', () => signIn(true));
$('#refresh').addEventListener('click', () => refresh(true).catch(refreshFailed));
$('#sign-out').addEventListener('click', async () => {
  try { await api('/api/chatgpt/sign-out', {}); await refresh(); } catch (exc) { error(exc.message); }
});
window.addEventListener('pagehide', () => { if (allowed) disconnect(); });

if (!parent || !validOrigin(origin) || !/^[a-zA-Z0-9-]{20,80}$/.test(channel || '')) {
  $('#approval').hidden = true;
  error('Open this companion using Connect ChatGPT on the Effect Size Finder website.');
} else {
  $('#website').textContent = origin;
  try { accessKey = localStorage.getItem('esf-access-key') || ''; } catch { /* api explains missing access */ }
  send({ kind: 'waiting' });
  try {
    await refresh();
    $('#allow').disabled = false;
  } catch (exc) { error(exc.message); }
}
