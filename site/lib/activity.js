// What the agents are doing. Finished steps go to the search's activity log in IndexedDB; steps in
// progress are kept in memory, so the page can show who is working on what right now.
//
// agent: 'opus' (manager) | 'sonnet' (fetcher) | 'haiku' (workers) | 'code' (searches and checks)

import { describeError } from './claude.js';

const live = new Map(); // projectId -> Map(taskId -> {agent, step, text, started})
let nextId = 1;

// Steps of a search that are running now, oldest first.
export const liveTasks = (projectId) => [...(live.get(projectId)?.values() || [])];

export const short = (text, n = 70) => {
  const s = String(text || '').replace(/\s+/g, ' ').trim();
  return s.length > n ? `${s.slice(0, n - 1)}…` : s;
};

export class Activity {
  constructor(store, projectId) {
    this.store = store;
    this.projectId = projectId;
  }

  // Who is working changed. Only the agent cards need redrawing, so this doesn't touch storage.
  liveChanged() {
    this.store.dispatchEvent(new CustomEvent('live', { detail: { projectId: this.projectId } }));
  }

  async log(event) {
    try {
      await this.store.addEvent(this.projectId, event);
    } catch (error) {
      console.warn('Could not record activity:', error); // the log is a convenience; never stop the work for it
    }
  }

  // Run `work` as a step of `agent`; `summarize(result)` says how it ended.
  async track({ agent, step, text, paperId = null }, work, summarize = null) {
    const id = nextId++;
    if (!live.has(this.projectId)) live.set(this.projectId, new Map());
    const tasks = live.get(this.projectId);
    tasks.set(id, { id, agent, step, text, started: Date.now() });
    this.liveChanged();
    const started = performance.now();
    const ms = () => Math.round(performance.now() - started);
    try {
      const result = await work();
      let detail = '';
      try {
        detail = summarize ? summarize(result) : '';
      } catch { /* a summary is optional */ }
      await this.log({ agent, step, phase: 'done', text, detail, ms: ms(), paper_id: paperId });
      return result;
    } catch (error) {
      await this.log({ agent, step, phase: 'error', text, detail: describeError(error), ms: ms(), paper_id: paperId });
      throw error;
    } finally {
      tasks.delete(id);
      this.liveChanged();
    }
  }

  note({ agent, step, text, detail = '', paperId = null, phase = 'note' }) {
    return this.log({ agent, step, phase, text, detail, ms: null, paper_id: paperId });
  }
}

// For callers that don't keep a log.
export const NO_ACTIVITY = { track: (_, work) => work(), note: async () => {} };
