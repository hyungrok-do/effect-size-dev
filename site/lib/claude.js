// Claude, called from the browser with the user's own API key.
//
//   Opus 5.5   (manager)  reads the problem, plans the search, re-extracts escalated papers
//   Sonnet 5.5 (fetcher)  downloads what the browser may not: PDFs and sites that refuse web pages
//   Haiku 5.5  (workers)  screens every record and reads every paper
//
// Prompts, schemas and search tools come from the Python package (generated.js), so both editions
// give Claude the same instructions. Claude only reads and quotes; code does any arithmetic.

import { Anthropic } from '../vendor/anthropic-sdk.mjs';
import { LEVELS, MAX_COUNTED, PROMPTS, SCHEMAS, SEARCH_PLAN_RECORDED, SEARCH_TOOLS } from './generated.js';
import { SourceError } from './sources.js';

export const MODELS = { manager: 'claude-opus-5-5', fetcher: 'claude-sonnet-5-5', worker: 'claude-haiku-5-5' };

// Opus and Sonnet: when a safety classifier declines a benign request, the API retries it on the
// model Anthropic recommends for that refusal category. Haiku has no server-side fallback.
const FALLBACK = { betas: ['server-side-fallback-2026-07-01'], fallbacks: 'default' };

export class ModelDeclined extends Error {}
export class ModelTruncated extends Error {}

// Anthropic's own words for a failed request, e.g. "401: invalid x-api-key (request req_011…)".
function apiDetail(error) {
  const body = error?.error?.error || error?.error;
  const text = typeof body?.message === 'string' ? body.message : error?.message || String(error);
  const id = error?.requestID || error?.error?.request_id;
  return `${error?.status ? `${error.status}: ` : ''}${text}${id ? ` (request ${id})` : ''}`;
}

export function describeError(error) {
  const message = error?.message || String(error);
  if (error?.name === 'OpenAIError') return message; // openai.js words its own errors
  if (/Could not resolve authentication method/i.test(message)) {
    return 'No Anthropic API key was given. Enter your key at the top of the page and try again.';
  }
  if (error?.status === 401) {
    return `Anthropic did not accept the API key (${apiDetail(error)}). Check the key at the top of the page; Anthropic keys start with sk-ant-.`;
  }
  if (error?.status === 403 || error?.status === 404) {
    return `Anthropic refused the request (${apiDetail(error)}). Check that the key's workspace may use this model.`;
  }
  if (error?.status === 429) return `The Anthropic API rate limit was reached (${apiDetail(error)}). Wait a minute and try again.`;
  if (error?.status) return `The Anthropic API returned an error (${apiDetail(error)}).`;
  return message;
}

export const looksLikeAnthropicKey = (key) => /^sk-ant-[A-Za-z0-9_-]{8,}$/.test(key);

// What the search agent's tools do, by name, for either provider; `plan()` is the plan it submitted.
// A null max_results (OpenAI's strict mode sends null for "the default") takes the default.
export function searchRunners(toolbox) {
  let submitted = null;
  const preview = (input) => Math.max(1, Math.min(input.max_results ?? 20, 50));
  const tools = {
    count_pubmed: (input) => toolbox.countPubmed((input.queries || []).slice(0, MAX_COUNTED)),
    search_pubmed: (input) => toolbox.previewPubmed(input.query, preview(input)),
    search_europepmc: (input) => toolbox.previewEuropepmc(input.query, preview(input)),
    search_clinicaltrials: (input) => toolbox.previewTrials(input.query, preview(input)),
    submit_search_plan: (input) => {
      const levels = input.levels || [];
      // The Python edition validates the input against the tool's schema; refuse the same things here.
      if (!levels.every((l) => LEVELS.includes(l?.level) && typeof l.query === 'string')) {
        throw new Error(`Each level needs a level (${LEVELS.join(', ')}) and a query.`);
      }
      submitted = {
        levels: levels.map((l) => ({ level: l.level, query: l.query, why: l.why || '' })),
        clinicaltrials_queries: input.clinicaltrials_queries || [],
        extra_ids: input.extra_ids || [],
        summary: input.summary || '',
      };
      return SEARCH_PLAN_RECORDED;
    },
  };
  return { tools, plan: () => submitted };
}

const cachedSystem = (text) => [{ type: 'text', text, cache_control: { type: 'ephemeral' } }];
const withSpec = (prompt, spec) => `${prompt}\n\nSpecification:\n${JSON.stringify(spec, null, 2)}`;

// The structured output is the text after any fallback switch point.
function structured(message) {
  if (message.stop_reason === 'refusal') {
    throw new ModelDeclined(`declined (${message.stop_details?.category || 'no category'})`);
  }
  if (message.stop_reason === 'max_tokens') throw new ModelTruncated('the response hit max_tokens');
  const lastSwitch = message.content.map((b) => b.type).lastIndexOf('fallback');
  const text = message.content.slice(lastSwitch + 1).filter((b) => b.type === 'text').map((b) => b.text).join('');
  return JSON.parse(text);
}

export class Claude {
  // Claude can fetch what the browser may not (fetchUrl); OpenAI has no such tool.
  canFetch = true;

  constructor({ apiKey, models = MODELS, client = null }) {
    this.models = models;
    this.client = client || new Anthropic({ apiKey, dangerouslyAllowBrowser: true, maxRetries: 3 });
  }

  // Does the key work, and may it use each model? Model lookups cost nothing.
  async checkAccess() {
    const results = [];
    for (const [role, model] of Object.entries(this.models)) {
      try {
        const info = await this.client.models.retrieve(model);
        results.push({ role, model, ok: true, name: info.display_name || model });
      } catch (error) {
        results.push({ role, model, ok: false, status: error?.status, error: describeError(error) });
        if (error?.status === 401) break; // the key itself was refused; the other lookups would say the same
      }
    }
    return results;
  }

  static record(sink, step, message) {
    if (sink && message?.usage) sink(step, message.model, message.usage);
  }

  // Streamed, so long documents and high-effort thinking don't run into request timeouts.
  async parse({ step, manager, system, content, schema, effort, sink, maxTokens = 64000 }) {
    const params = {
      model: manager ? this.models.manager : this.models.worker,
      max_tokens: maxTokens,
      system: cachedSystem(system),
      messages: [{ role: 'user', content }],
      output_config: { effort, format: { type: 'json_schema', schema } },
      ...(manager ? FALLBACK : {}),
    };
    const message = await this.client.beta.messages.stream(params).finalMessage();
    Claude.record(sink, step, message);
    return structured(message);
  }

  // -- manager (Opus) ---------------------------------------------------------------

  readSpec(paragraph, { sink } = {}) {
    return this.parse({ step: 'spec', manager: true, system: PROMPTS.SPEC, content: paragraph, schema: SCHEMAS.SearchSpec, effort: 'high', sink });
  }

  async planSearch(spec, toolbox, { sink, maxIterations = 25 } = {}) {
    const { tools, plan } = searchRunners(toolbox);
    const messages = [{ role: 'user', content: `Specification:\n${JSON.stringify(spec, null, 2)}` }];
    for (let i = 0; i < maxIterations; i++) {
      const message = await this.client.beta.messages.create({
        model: this.models.manager,
        max_tokens: 16000,
        system: cachedSystem(PROMPTS.SEARCH),
        tools: SEARCH_TOOLS,
        messages,
        output_config: { effort: 'high' },
        ...FALLBACK,
      });
      Claude.record(sink, 'search', message);
      if (message.stop_reason === 'refusal') throw new ModelDeclined("the search agent's request was declined");
      messages.push({ role: 'assistant', content: message.content }); // append-only: thinking blocks stay valid
      const uses = message.content.filter((b) => b.type === 'tool_use');
      if (message.stop_reason !== 'tool_use' || !uses.length) break;
      // All results go back in one user message, so Claude keeps calling tools in parallel.
      const results = await Promise.all(uses.map(async (use) => {
        const run = tools[use.name];
        if (!run) return { type: 'tool_result', tool_use_id: use.id, content: `Error: Tool '${use.name}' not found`, is_error: true };
        try {
          return { type: 'tool_result', tool_use_id: use.id, content: String(await run(use.input || {})) };
        } catch (error) {
          return { type: 'tool_result', tool_use_id: use.id, content: `Error: ${describeError(error)}`, is_error: true };
        }
      }));
      messages.push({ role: 'user', content: results });
    }
    if (!plan()) throw new Error('The search agent stopped without submitting a search plan.');
    return plan();
  }

  checkOverlap(spec, records, { sink } = {}) {
    return this.parse({
      step: 'overlap', manager: true, system: withSpec(PROMPTS.OVERLAP, spec),
      content: [{ type: 'text', text: `Records:\n${JSON.stringify(records, null, 1)}` }], schema: SCHEMAS.OverlapReport, effort: 'high', sink,
    });
  }

  summarize(spec, overlap, evidence, { sink } = {}) {
    const data = { records: evidence.records, overlap, values: evidence.values };
    return this.parse({
      step: 'summarize', manager: true, system: withSpec(PROMPTS.SUMMARIZE, spec),
      content: [{ type: 'text', text: `Records, the overlap check and the values:\n${JSON.stringify(data, null, 1)}` }],
      schema: SCHEMAS.EvidenceSummary, effort: 'high', sink,
    });
  }

  adjudicate(spec, document, worker, flags, { sink } = {}) {
    const junior = worker ? JSON.stringify(worker, null, 1) : 'None: the junior reviewer could not complete it.';
    const text = `Junior reviewer's extraction:\n${junior}\n\nFlags from the automatic checks:\n${flags.map((f) => `- ${f}`).join('\n')}`;
    return this.parse({
      step: 'adjudicate', manager: true, system: withSpec(PROMPTS.ADJUDICATE, spec),
      content: [document, { type: 'text', text }], schema: SCHEMAS.PaperExtraction, effort: 'xhigh', sink,
    });
  }

  // -- fetcher (Sonnet) -------------------------------------------------------------

  // Download one address on Anthropic's servers, where browser restrictions don't apply. One fetch
  // per call: nothing inside the fetched document can lead Sonnet to fetch anything else.
  async fetchUrl(url, { sink } = {}) {
    const message = await this.client.beta.messages.create({
      model: this.models.fetcher,
      max_tokens: 4000,
      output_config: { effort: 'low' },
      tools: [{ type: 'web_fetch_20250910', name: 'web_fetch', max_uses: 1 }],
      messages: [{
        role: 'user',
        content: `Fetch ${url} with the web_fetch tool. Do not summarize or analyze it; once it is fetched, reply only "done".`,
      }],
      ...FALLBACK,
    });
    Claude.record(sink, 'fetch', message);
    const result = message.content.find((b) => b.type === 'web_fetch_tool_result');
    if (!result) {
      if (message.stop_reason === 'refusal') throw new SourceError('Claude declined to fetch the address.');
      throw new SourceError('Claude did not fetch the address.');
    }
    if (result.content?.type === 'web_fetch_tool_result_error') {
      throw new SourceError(`Claude could not fetch the address (${result.content.error_code}).`);
    }
    const source = result.content?.content?.source;
    if (source?.type === 'base64' && source.media_type === 'application/pdf') return { kind: 'pdf', base64: source.data };
    if (source?.type === 'text') return { kind: 'text', text: source.data };
    throw new SourceError('Claude fetched something that is neither text nor a PDF.');
  }

  // -- workers (Haiku) --------------------------------------------------------------

  screen(spec, recordText, { sink } = {}) {
    return this.parse({
      step: 'screen', manager: false, system: withSpec(PROMPTS.SCREEN, spec), content: recordText,
      schema: SCHEMAS.ScreenDecision, effort: 'low', sink, maxTokens: 8000,
    });
  }

  extract(spec, document, { sink } = {}) {
    return this.parse({
      step: 'extract', manager: false, system: withSpec(PROMPTS.EXTRACT, spec),
      content: [document, { type: 'text', text: 'Extract the statistics relevant to the specification.' }],
      schema: SCHEMAS.PaperExtraction, effort: 'medium', sink,
    });
  }
}
