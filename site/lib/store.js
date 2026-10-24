// Searches, papers, review decisions, token usage and retrieved documents, kept in this browser's
// IndexedDB. Nothing is stored anywhere else. Mirrors python/esf/store.py.

const DB_NAME = 'effect-size-finder';
const VERSION = 2; // 2 added the agent activity log
export const RUNNING = new Set(['reading', 'searching', 'screening', 'extracting']);

const now = () => new Date().toISOString().replace(/\.\d+Z$/, 'Z');
const request = (req) => new Promise((resolve, reject) => {
  req.onsuccess = () => resolve(req.result);
  req.onerror = () => reject(req.error);
});

export class Store extends EventTarget {
  constructor(name = DB_NAME) {
    super();
    this.ready = new Promise((resolve, reject) => {
      const open = indexedDB.open(name, VERSION);
      open.onupgradeneeded = (event) => {
        const db = open.result;
        if (event.oldVersion < 1) {
          db.createObjectStore('projects', { keyPath: 'id' });
          const papers = db.createObjectStore('papers', { keyPath: 'id', autoIncrement: true });
          papers.createIndex('project', 'project_id');
          papers.createIndex('key', ['project_id', 'source', 'ext_id'], { unique: true });
          const decisions = db.createObjectStore('decisions', { keyPath: ['paper_id', 'result_index'] });
          decisions.createIndex('project', 'project_id');
          db.createObjectStore('usage', { autoIncrement: true }).createIndex('project', 'project_id');
          db.createObjectStore('documents', { keyPath: 'paper_id' }).createIndex('project', 'project_id');
        }
        if (event.oldVersion < 2) {
          db.createObjectStore('events', { keyPath: 'id', autoIncrement: true }).createIndex('project', 'project_id');
        }
      };
      open.onsuccess = () => resolve(open.result);
      open.onerror = () => reject(open.error);
    });
  }

  async tx(names, mode, fn) {
    const db = await this.ready;
    const t = db.transaction(names, mode);
    const done = new Promise((resolve, reject) => {
      t.oncomplete = resolve;
      t.onerror = () => reject(t.error);
      t.onabort = () => reject(t.error);
    });
    const stores = Object.fromEntries([names].flat().map((n) => [n, t.objectStore(n)]));
    let result;
    try {
      result = await fn(stores);
    } catch (error) {
      done.catch(() => {}); // the transaction aborts; the caller gets the original error
      throw error;
    }
    await done;
    return result;
  }

  changed(projectId) {
    this.dispatchEvent(new CustomEvent('change', { detail: { projectId } }));
  }

  // -- projects -------------------------------------------------------------------

  async createProject(paragraph) {
    const id = crypto.randomUUID().replaceAll('-', '').slice(0, 12);
    await this.tx('projects', 'readwrite', ({ projects }) => request(projects.add({
      id, created_at: now(), paragraph, spec: null, status: 'new', progress: null, error: null, search_plan: null,
    })));
    this.changed(id);
    return id;
  }

  project(id) {
    return this.tx('projects', 'readonly', ({ projects }) => request(projects.get(id)));
  }

  async projects() {
    const all = await this.tx('projects', 'readonly', ({ projects }) => request(projects.getAll()));
    return all.sort((a, b) => b.created_at.localeCompare(a.created_at));
  }

  async updateProject(id, fields) {
    await this.tx('projects', 'readwrite', async ({ projects }) => {
      const project = await request(projects.get(id));
      if (project) await request(projects.put({ ...project, ...fields }));
    });
    this.changed(id);
  }

  // On page load no job is running in this tab, so anything marked running was cut short.
  async interruptRunning() {
    for (const p of await this.projects()) {
      if (RUNNING.has(p.status)) {
        await this.updateProject(p.id, { status: 'interrupted', error: 'The page was closed or reloaded while this was running.' });
      }
      if (p.summary?.status === 'running') {
        const error = 'The page was closed or reloaded while the summary was being written.';
        const { previous } = p.summary;
        await this.updateProject(p.id, { summary: previous ? { ...previous, last_error: error } : { status: 'error', error } });
      }
    }
    // Papers cut off mid-extraction go back to where they were, so Continue picks them up again.
    const before = { include: 'screened_in', unsure: 'unsure', exclude: 'screened_out' };
    await this.tx('papers', 'readwrite', async ({ papers }) => {
      for (const paper of await request(papers.getAll())) {
        if (paper.status === 'fetching' || paper.status === 'extracting') {
          await request(papers.put({
            ...paper, status: before[paper.screen?.decision] || 'screened_in',
            error: 'The page was closed or reloaded before this was extracted.',
          }));
        }
      }
    });
  }

  async deleteProject(id) {
    await this.clearPapers(id);
    await this.tx(['projects', 'usage', 'events'], 'readwrite', async ({ projects, usage, events }) => {
      await request(projects.delete(id));
      for (const key of await request(usage.index('project').getAllKeys(id))) await request(usage.delete(key));
      for (const key of await request(events.index('project').getAllKeys(id))) await request(events.delete(key));
    });
    this.changed(id);
  }

  // -- papers ---------------------------------------------------------------------

  // Insert a paper; returns null when the project already has it.
  async addPaper(projectId, fields) {
    try {
      return await this.tx('papers', 'readwrite', ({ papers }) => request(papers.add({ project_id: projectId, ...fields })));
    } catch (error) {
      if (error?.name === 'ConstraintError') return null;
      throw error;
    }
  }

  paper(id) {
    return this.tx('papers', 'readonly', ({ papers }) => request(papers.get(id)));
  }

  async papers(projectId, status = null) {
    const all = await this.tx('papers', 'readonly', ({ papers }) => request(papers.index('project').getAll(projectId)));
    return all.filter((p) => !status || p.status === status).sort((a, b) => a.id - b.id);
  }

  async updatePaper(id, fields) {
    let projectId = null;
    await this.tx('papers', 'readwrite', async ({ papers }) => {
      const paper = await request(papers.get(id));
      if (paper) {
        projectId = paper.project_id;
        await request(papers.put({ ...paper, ...fields }));
      }
    });
    if (projectId) this.changed(projectId);
  }

  async clearPapers(projectId) {
    await this.tx(['papers', 'decisions', 'documents'], 'readwrite', async ({ papers, decisions, documents }) => {
      for (const key of await request(papers.index('project').getAllKeys(projectId))) await request(papers.delete(key));
      for (const key of await request(decisions.index('project').getAllKeys(projectId))) await request(decisions.delete(key));
      for (const key of await request(documents.index('project').getAllKeys(projectId))) await request(documents.delete(key));
    });
    this.changed(projectId);
  }

  async statusCounts(projectId) {
    const counts = {};
    for (const p of await this.papers(projectId)) counts[p.status] = (counts[p.status] || 0) + 1;
    return counts;
  }

  // -- decisions ------------------------------------------------------------------

  async setDecision(projectId, paperId, resultIndex, decision, reason = '') {
    await this.tx('decisions', 'readwrite', ({ decisions }) => request(decision
      ? decisions.put({ project_id: projectId, paper_id: paperId, result_index: resultIndex, decision, reason, decided_at: now() })
      : decisions.delete([paperId, resultIndex])));
    this.changed(projectId);
  }

  async clearDecisions(paperId) {
    await this.tx('decisions', 'readwrite', async ({ decisions }) => {
      for (const d of await request(decisions.getAll())) {
        if (d.paper_id === paperId) await request(decisions.delete([d.paper_id, d.result_index]));
      }
    });
  }

  // { paperId: { resultIndex: {decision, reason, decided_at} } }
  async decisions(projectId) {
    const rows = await this.tx('decisions', 'readonly', ({ decisions }) => request(decisions.index('project').getAll(projectId)));
    const out = {};
    for (const r of rows) (out[r.paper_id] ||= {})[r.result_index] = { decision: r.decision, reason: r.reason, decided_at: r.decided_at };
    return out;
  }

  // -- usage ----------------------------------------------------------------------

  async addUsage(projectId, step, model, usage) {
    await this.tx('usage', 'readwrite', ({ usage: store }) => request(store.add({
      project_id: projectId, step, model, created_at: now(),
      input_tokens: usage.input_tokens || 0,
      output_tokens: usage.output_tokens || 0,
      cache_read_input_tokens: usage.cache_read_input_tokens || 0,
      cache_creation_input_tokens: usage.cache_creation_input_tokens || 0,
      web_fetch_requests: usage.server_tool_use?.web_fetch_requests || 0,
    })));
  }

  // Tokens by step and model; with `since`, only the calls from then on (and reading the problem).
  async usage(projectId, since = null) {
    const all = await this.tx('usage', 'readonly', ({ usage }) => request(usage.index('project').getAll(projectId)));
    const rows = since ? all.filter((r) => r.created_at >= since || r.step === 'spec') : all;
    const groups = new Map();
    for (const r of rows.sort((a, b) => a.created_at.localeCompare(b.created_at))) {
      const key = `${r.step}\u0001${r.model}`;
      const g = groups.get(key) || {
        step: r.step, model: r.model, calls: 0, input_tokens: 0, output_tokens: 0,
        cache_read_input_tokens: 0, cache_creation_input_tokens: 0, web_fetch_requests: 0,
      };
      g.calls += 1;
      for (const k of ['input_tokens', 'output_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens', 'web_fetch_requests']) g[k] += r[k] || 0;
      groups.set(key, g);
    }
    return [...groups.values()];
  }

  // -- agent activity ---------------------------------------------------------------

  // event: {agent, step, phase: 'done' | 'error' | 'note', text, detail, ms, paper_id, usage, models, job, parent, started}
  async addEvent(projectId, event) {
    await this.tx('events', 'readwrite', ({ events }) => request(events.add({
      project_id: projectId, at: new Date().toISOString(), ...event,
    })));
    this.changed(projectId);
  }

  // Every event of a search, newest first.
  async events(projectId) {
    const all = await this.tx('events', 'readonly', ({ events }) => request(events.index('project').getAll(IDBKeyRange.only(projectId))));
    return all.reverse();
  }

  // -- documents ------------------------------------------------------------------

  saveDocument(projectId, paperId, doc) {
    return this.tx('documents', 'readwrite', ({ documents }) => request(documents.put({ project_id: projectId, paper_id: paperId, ...doc })));
  }

  document(paperId) {
    return this.tx('documents', 'readonly', ({ documents }) => request(documents.get(paperId)));
  }
}
