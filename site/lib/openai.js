// OpenAI models, called from the browser with the user's own API key.
//
//   manager  reads the problem, plans the search, checks overlap, summarizes, re-extracts escalated papers,
//            reads each paper's estimands and models
//   workers  screen every record and read every paper
//
// The same steps, prompts and schemas as claude.js (generated.js comes from the Python package), and
// the same requests as python/esf/openai_llm.py, which the local edition also runs on a ChatGPT
// plan: not stored and streamed, the system prompt in `instructions`, no output-token cap. OpenAI has
// no counterpart to Claude's server-side fetch, so there is no fetcher: a PDF the browser may not
// download is left for the user to upload. The models only read and quote; code does any arithmetic.

import { contextInput, ModelDeclined, ModelTruncated, searchRunners } from './claude.js';
import { OPENAI_TOOLS, PROMPTS, SCHEMAS, TOOL_NAMESPACE } from './generated.js';
import { modelLabel } from './providers.js';

export const OPENAI_MODELS = { manager: 'gpt-6.1-sol', worker: 'gpt-6-luna' };
const API = 'https://api.openai.com/v1';
const MAX_SERVER_MESSAGE = 300;

export class OpenAIError extends Error {
  constructor(code, message, { status = null, retryable = false, requestId = null } = {}) {
    super(message);
    this.name = 'OpenAIError';
    this.code = code;
    this.status = status;
    this.retryable = retryable;
    this.requestId = requestId;
  }
}

export { looksLikeOpenAIKey } from './providers.js';

// -- what goes into a request (mirrors python/esf/openai_llm.py) -----------------------------

export function pdfName(title) {
  const stem = String(title).replace(/[^\p{L}\p{N}_ .-]+/gu, '').slice(0, 80).replace(/^[ .]+|[ .]+$/g, '');
  return `${stem || 'document'}.pdf`;
}

// Claude message content (a string, or text and document blocks) as Responses input parts.
export function inputContent(content) {
  if (typeof content === 'string') return [{ type: 'input_text', text: content }];
  return content.map((block) => {
    if (block.type === 'text') return { type: 'input_text', text: block.text };
    if (block.type === 'document') {
      const { source } = block;
      const title = block.title || 'Document';
      if (source.type === 'base64' && source.media_type === 'application/pdf') {
        return { type: 'input_file', filename: pdfName(title), file_data: `data:application/pdf;base64,${source.data}` };
      }
      return { type: 'input_text', text: `Document: ${title}\n\n${source.data}` };
    }
    throw new Error(`Unsupported content block: ${block.type}`);
  });
}

// OpenAI usage in the fields the store keeps for Claude: cached input counted apart.
export function usageOf(usage = {}) {
  const cached = usage?.input_tokens_details?.cached_tokens || 0;
  return {
    input_tokens: Math.max(0, (usage?.input_tokens || 0) - cached),
    output_tokens: usage?.output_tokens || 0,
    cache_read_input_tokens: cached,
    cache_creation_input_tokens: 0,
  };
}

const KNOWN_ERRORS = {
  invalid_api_key: ['OpenAI did not accept the API key. Check it at the top of the page; OpenAI keys start with sk-.', false],
  model_not_found: ['This model is not available to this API key. Check the key\'s project and model access.', false],
  insufficient_quota: ['The OpenAI account has run out of credit. Check its billing.', false],
  rate_limit_exceeded: ['The OpenAI rate limit was reached. Wait a minute and try again.', true],
  server_error: ['OpenAI had a server error. Try again shortly.', true],
};

export function apiError(body, status = null, requestId = null) {
  let detail = body && typeof body === 'object' ? body : {};
  for (let i = 0; i < 4 && detail.error && typeof detail.error === 'object'; i++) detail = detail.error;
  const code = (typeof detail.code === 'string' && detail.code) || (typeof detail.error === 'string' && detail.error)
    || (typeof detail.type === 'string' && detail.type) || 'api_error';
  let server = typeof detail.message === 'string' ? detail.message : '';
  if (server.length > MAX_SERVER_MESSAGE) server = `${server.slice(0, MAX_SERVER_MESSAGE)}…`;
  let [message, retryable] = KNOWN_ERRORS[code] || [];
  if (!message) {
    if (status === 401) [message, retryable] = ['OpenAI did not accept the API key. Check it at the top of the page.', false];
    else if (status === 403) [message, retryable] = ['OpenAI refused the request. Check that the key\'s project may use the model.', false];
    else if (status === 429) [message, retryable] = ['OpenAI asked to slow down. Wait a minute and try again.', true];
    else if (status >= 500) [message, retryable] = ['OpenAI is unavailable for the moment. Try again shortly.', true];
    else [message, retryable] = ['OpenAI could not complete the request.', false];
    if (server) message = `${message} OpenAI said: ${server}`;
  }
  const suffix = ` (${status || 'error'} ${code}${requestId ? `, request ${requestId}` : ''})`;
  return new OpenAIError(code, message + suffix, { status, retryable, requestId });
}

// -- the stream ----------------------------------------------------------------------------

async function* sseEvents(body) {
  const reader = body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = '';
  let data = [];
  const flush = function* () {
    const payload = data.join('\n').trim();
    data = [];
    if (payload && payload !== '[DONE]') yield JSON.parse(payload);
  };
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += value;
    let end;
    while ((end = buffer.indexOf('\n')) >= 0) {
      const line = buffer.slice(0, end).replace(/\r$/, '');
      buffer = buffer.slice(end + 1);
      if (line.startsWith('data:')) data.push(line.slice(5).replace(/^ /, ''));
      else if (line === '' && data.length) yield* flush();
    }
  }
  if (buffer.startsWith('data:')) data.push(buffer.slice(5).replace(/^ /, ''));
  if (data.length) yield* flush();
}

// The finished response from a Responses stream: its output items, status and usage.
export async function readStream(body, requestId = null) {
  const items = [];
  for await (const event of sseEvents(body)) {
    if (event.type === 'response.output_item.done' && event.item) items.push(event.item);
    else if (event.type === 'response.completed' || event.type === 'response.incomplete') {
      const response = { ...(event.response || {}) };
      response.output = items.length ? items : response.output || [];
      return response;
    } else if (event.type === 'response.failed') throw apiError({ error: event.response?.error || {} }, null, requestId);
    else if (event.type === 'error') throw apiError(event, null, requestId);
  }
  throw new OpenAIError('stream_interrupted', 'The answer from OpenAI stopped before it finished. Try again.', { retryable: true, requestId });
}

// The text of a finished response; a refusal or an early stop throws.
export function outputText(response) {
  if (response.status === 'incomplete') {
    const reason = response.incomplete_details?.reason || 'incomplete';
    if (reason === 'content_filter') throw new ModelDeclined('declined (content filter)');
    throw new ModelTruncated(`the response stopped early (${reason})`);
  }
  let text = '';
  for (const item of response.output || []) {
    if (item.type !== 'message') continue;
    for (const part of item.content || []) {
      if (part.type === 'refusal') throw new ModelDeclined(`declined (${(part.refusal || 'no reason given').slice(0, 200)})`);
      if (part.type === 'output_text') text += part.text || '';
    }
  }
  return text;
}

const withSpec = (prompt, spec) => `${prompt}\n\nSpecification:\n${JSON.stringify(spec, null, 2)}`;
const wait = (seconds) => new Promise((resolve) => { setTimeout(resolve, seconds * 1000); });

export class OpenAI {
  canFetch = false;

  constructor({ apiKey, models = OPENAI_MODELS, fetch = (...args) => globalThis.fetch(...args), retries = 3, sleep = wait }) {
    this.apiKey = apiKey;
    this.models = models;
    this.fetch = fetch;
    this.retries = retries;
    this.sleep = sleep;
  }

  headers(extra = {}) {
    return { Authorization: `Bearer ${this.apiKey}`, ...extra };
  }

  // Does the key work, and may it use each model? Model lookups cost nothing.
  async checkAccess() {
    const results = [];
    for (const [role, model] of Object.entries(this.models)) {
      try {
        const response = await this.fetch(`${API}/models/${encodeURIComponent(model)}`, { headers: this.headers() });
        if (!response.ok) throw apiError(await response.json().catch(() => null), response.status, response.headers.get('x-request-id'));
        results.push({ role, model, ok: true, name: modelLabel(model) });
      } catch (error) {
        results.push({ role, model, ok: false, status: error?.status, error: error?.message || String(error) });
        if (error?.status === 401) break; // the key itself was refused; the other lookups would say the same
      }
    }
    return results;
  }

  static record(sink, step, model, response) {
    if (sink && response?.usage) sink(step, response.model || model, usageOf(response.usage));
  }

  async create(body) {
    for (let attempt = 0; ; attempt++) {
      try {
        return await this.once(body);
      } catch (error) {
        const network = error instanceof TypeError; // fetch's way of saying the request never got an answer
        if (!(network || error?.retryable) || attempt >= this.retries) {
          if (network) throw new OpenAIError('network_error', `Could not reach OpenAI (${error.message}). Check the connection and try again.`, { retryable: true });
          throw error;
        }
      }
      await this.sleep(Math.min(2 ** attempt, 8));
    }
  }

  async once(body) {
    const response = await this.fetch(`${API}/responses`, {
      method: 'POST',
      headers: this.headers({ Accept: 'text/event-stream', 'Content-Type': 'application/json' }),
      body: JSON.stringify(body),
    });
    const requestId = response.headers.get('x-request-id');
    if (!response.ok) throw apiError(await response.json().catch(() => null), response.status, requestId);
    return readStream(response.body, requestId);
  }

  body(model, system, input, effort) {
    return { model, instructions: system, input, reasoning: { effort }, store: false, stream: true };
  }

  async parse({ step, manager, system, content, schema, name, effort, sink }) {
    const model = manager ? this.models.manager : this.models.worker;
    const body = this.body(model, system, [{ role: 'user', content: inputContent(content) }], effort);
    body.text = { format: { type: 'json_schema', name, schema, strict: true } };
    const response = await this.create(body);
    OpenAI.record(sink, step, model, response);
    return JSON.parse(outputText(response));
  }

  // -- manager --------------------------------------------------------------------------------

  readSpec(paragraph, { sink } = {}) {
    return this.parse({ step: 'spec', manager: true, system: PROMPTS.SPEC, content: paragraph, schema: SCHEMAS.SearchSpec, name: 'SearchSpec', effort: 'high', sink });
  }

  // `namespaced`: plan usage takes function tools only inside a namespace; an API key takes either,
  // and the browser edition sends them plain.
  async planSearch(spec, toolbox, { sink, maxIterations = 25, namespaced = false } = {}) {
    const { tools, plan } = searchRunners(toolbox);
    const definitions = namespaced
      ? [{ type: 'namespace', name: TOOL_NAMESPACE, description: 'Literature searches for planning the search.', tools: OPENAI_TOOLS }]
      : OPENAI_TOOLS;
    const model = this.models.manager;
    const input = [{ role: 'user', content: [{ type: 'input_text', text: `Specification:\n${JSON.stringify(spec, null, 2)}` }] }];
    for (let i = 0; i < maxIterations; i++) {
      const body = this.body(model, PROMPTS.SEARCH, input, 'high');
      body.tools = definitions;
      // Not stored, so the reasoning comes back encrypted to send with the next turn.
      body.include = ['reasoning.encrypted_content'];
      const response = await this.create(body);
      OpenAI.record(sink, 'search', model, response);
      outputText(response); // a refusal or an early stop throws
      input.push(...response.output);
      const calls = response.output.filter((item) => item.type === 'function_call');
      if (!calls.length) break;
      // The searches run in parallel; their results go back in the order the model asked.
      const outputs = await Promise.all(calls.map((call) => runTool(tools, call)));
      calls.forEach((call, n) => input.push({ type: 'function_call_output', call_id: call.call_id, output: outputs[n] }));
    }
    if (!plan()) throw new Error('The search agent stopped without submitting a search plan.');
    return plan();
  }

  checkOverlap(spec, records, { sink } = {}) {
    return this.parse({
      step: 'overlap', manager: true, system: withSpec(PROMPTS.OVERLAP, spec),
      content: [{ type: 'text', text: `Records:\n${JSON.stringify(records, null, 1)}` }], schema: SCHEMAS.OverlapReport, name: 'OverlapReport', effort: 'high', sink,
    });
  }

  summarize(spec, overlap, evidence, { sink } = {}) {
    const data = { records: evidence.records, estimands: evidence.estimands, overlap, values: evidence.values };
    return this.parse({
      step: 'summarize', manager: true, system: withSpec(PROMPTS.SUMMARIZE, spec),
      content: [{ type: 'text', text: `Records, the estimands of their results, the overlap check and the values:\n${JSON.stringify(data, null, 1)}` }],
      schema: SCHEMAS.EvidenceSummary, name: 'EvidenceSummary', effort: 'high', sink,
    });
  }

  adjudicate(spec, document, worker, flags, { sink } = {}) {
    const junior = worker ? JSON.stringify(worker, null, 1) : 'None: the junior reviewer could not complete it.';
    const text = `Junior reviewer's extraction:\n${junior}\n\nFlags from the automatic checks:\n${flags.map((f) => `- ${f}`).join('\n')}`;
    return this.parse({
      step: 'adjudicate', manager: true, system: withSpec(PROMPTS.ADJUDICATE, spec),
      content: [document, { type: 'text', text }], schema: SCHEMAS.PaperExtraction, name: 'PaperExtraction', effort: 'xhigh', sink,
    });
  }

  studyContext(spec, document, extraction, { sink } = {}) {
    return this.parse({
      step: 'context', manager: true, system: withSpec(PROMPTS.CONTEXT, spec),
      content: [document, { type: 'text', text: contextInput(extraction) }], schema: SCHEMAS.StudyContext, name: 'StudyContext', effort: 'high', sink,
    });
  }

  // -- workers --------------------------------------------------------------------------------

  screen(spec, recordText, { sink } = {}) {
    return this.parse({
      step: 'screen', manager: false, system: withSpec(PROMPTS.SCREEN, spec), content: recordText,
      schema: SCHEMAS.ScreenDecision, name: 'ScreenDecision', effort: 'low', sink,
    });
  }

  extract(spec, document, { sink } = {}) {
    return this.parse({
      step: 'extract', manager: false, system: withSpec(PROMPTS.EXTRACT, spec),
      content: [document, { type: 'text', text: 'Extract the statistics relevant to the specification.' }],
      schema: SCHEMAS.PaperExtraction, name: 'PaperExtraction', effort: 'medium', sink,
    });
  }
}

// Run one function call; a failure goes back to the model as text, as Claude's tool results do.
async function runTool(tools, call) {
  const name = call.name || '';
  const run = tools[name] || tools[name.replace(new RegExp(`^${TOOL_NAMESPACE}[.:/_]+`), '')];
  if (!run) return `Error: Tool '${name}' not found`;
  try {
    return String(await run(JSON.parse(call.arguments || '{}')));
  } catch (error) {
    return `Error: ${error?.message || error}`;
  }
}
