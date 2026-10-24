// Browser-side connection to the local companion. Only model inputs/results and account display
// information cross this channel; the server access key and OpenAI credentials never do.
import { OpenAI, OpenAIError } from './openai.js';

const PROTOCOL = 'esf-chatgpt-v1';
const disconnected = (message = 'Connect ChatGPT in Agents before continuing.') =>
  new OpenAIError('bridge_disconnected', message, { status: 401 });

export function helperOrigin(address) {
  const url = new URL(address);
  if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1' || url.username || url.password
      || url.pathname !== '/' || url.search || url.hash) {
    throw new Error('Use the local helper address, for example http://127.0.0.1:8000.');
  }
  return url.origin;
}

export class ChatGPTConnection {
  constructor(onChange = () => {}) {
    this.onChange = onChange;
    this.phase = 'disconnected';
    this.state = null;
    this.error = '';
    this.popup = null;
    this.pending = new Map();
    window.addEventListener('message', (event) => this.receive(event));
    window.addEventListener('pagehide', () => this.disconnect());
  }

  get ready() {
    return this.phase === 'ready' && this.state?.status === 'connected' && Boolean(this.state.models?.length)
      && Boolean(this.popup) && !this.popup.closed;
  }

  connect(address) {
    if (this.popup && !this.popup.closed && this.phase === 'ready') {
      this.popup.focus();
      return;
    }
    const origin = helperOrigin(address);
    this.disconnect();
    this.origin = origin;
    this.channel = crypto.randomUUID();
    const fragment = new URLSearchParams({ origin: location.origin, channel: this.channel });
    this.popup = window.open(`${origin}/companion.html#${fragment}`, '_blank', 'popup,width=580,height=680');
    if (!this.popup) throw new Error('Allow this page to open the ChatGPT companion window, then connect again.');
    this.phase = 'connecting';
    this.error = '';
    this.onChange();
    this.startTimer = setTimeout(() => {
      this.error = 'The helper did not answer. Start esf serve, open its address in this browser, then connect again.';
      this.onChange();
    }, 10000);
    this.monitor = setInterval(() => {
      if (this.popup?.closed) this.disconnect('The companion window closed. Connect ChatGPT again to continue.');
    }, 1000);
  }

  receive(event) {
    const data = event.data;
    if (event.source !== this.popup || event.origin !== this.origin || data?.protocol !== PROTOCOL
        || data.channel !== this.channel) return;
    clearTimeout(this.startTimer);
    if (data.kind === 'waiting') {
      this.phase = 'waiting';
      this.error = '';
      this.onChange();
    } else if (data.kind === 'state') {
      const changed = this.phase !== 'ready' || this.error || JSON.stringify(this.state) !== JSON.stringify(data.state);
      this.phase = 'ready';
      this.state = data.state;
      this.error = '';
      if (changed) this.onChange();
    } else if (data.kind === 'closed') {
      this.disconnect('The companion disconnected. Connect ChatGPT again to continue.');
    } else if (data.kind === 'reply') {
      const pending = this.pending.get(data.id);
      if (!pending) return;
      this.pending.delete(data.id);
      clearTimeout(pending.timer);
      if (data.error) pending.reject(new OpenAIError(data.error.code || 'bridge_error', data.error.message,
        { status: data.error.status, retryable: Boolean(data.error.retryable) }));
      else pending.resolve(data.result);
    }
  }

  request(method, body = null) {
    if (this.phase !== 'ready' || this.popup?.closed || (method === 'responses' && !this.ready)) {
      return Promise.reject(disconnected());
    }
    return new Promise((resolve, reject) => {
      const id = crypto.randomUUID();
      // Responses may include several minutes of reasoning and the helper's transient-error retries.
      const timer = setTimeout(() => {
        this.disconnect('The companion stopped responding. Reconnect before continuing.');
      }, method === 'responses' ? 45 * 60 * 1000 : 60000);
      this.pending.set(id, { resolve, reject, timer });
      this.popup.postMessage({ protocol: PROTOCOL, channel: this.channel, kind: 'request', id, method, body }, this.origin);
    });
  }

  disconnect(message = '') {
    clearInterval(this.monitor);
    clearTimeout(this.startTimer);
    for (const pending of this.pending.values()) {
      clearTimeout(pending.timer);
      pending.reject(disconnected(message || undefined));
    }
    this.pending.clear();
    this.popup?.close();
    this.popup = null;
    this.phase = 'disconnected';
    this.state = null;
    this.error = message;
    this.onChange();
  }
}

// Same extraction and search steps as the OpenAI-key provider, billed to the signed-in plan.
export class ChatGPT extends OpenAI {
  pdfTextFallback = true;
  files = true;
  constructor({ connection, models }) {
    super({ apiKey: '', models, retries: 0 }); // the helper already retries transient API failures
    this.connection = connection;
  }

  once(body) {
    return this.connection.request('responses', body);
  }

  async parse(options) {
    const pdfs = Array.isArray(options.content) && options.content.filter((block) =>
      block.type === 'document' && block.source?.media_type === 'application/pdf');
    if (!pdfs?.length) return super.parse(options);
    if (this.files) {
      try { return await super.parse(options); }
      catch (error) {
        if (!error.code?.endsWith('unsupported_capability')) throw error;
        this.files = false;
      }
    }
    if (pdfs.some((block) => !block.text?.trim())) {
      throw new OpenAIError('pdf_not_readable', 'Your ChatGPT plan does not accept PDF files, and this PDF has no text layer. Upload a PDF with selectable text.');
    }
    const content = options.content.map((block) => pdfs.includes(block)
      ? { ...block, source: { type: 'text', media_type: 'text/plain', data: block.text } } : block);
    return super.parse({ ...options, content });
  }

  planSearch(spec, toolbox, options = {}) {
    return super.planSearch(spec, toolbox, { ...options, namespaced: true });
  }
}
