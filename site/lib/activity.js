// What the agents are doing. Finished steps go to the search's activity log in IndexedDB; steps in
// progress are kept in memory, so the page can show who is working on what right now.
//
// agent: 'opus' (manager) | 'sonnet' (fetcher) | 'haiku' (workers) | 'code' (searches and checks)

import { describeError } from './claude.js';

const live = new Map(); // projectId -> Map(taskId -> {id, agent, step, text, started, parent, usage, models})
let nextId = 1;
// A step's id in the log: unique across page loads, so the log can say which steps were part of which.
const RUN = Date.now().toString(36);
const jobId = (id) => (id == null ? null : `${RUN}-${id}`);
const MAX_DETAIL = 20000; // characters of a step's outcome kept in the log: all of it, short of a runaway

// Steps of a search that are running now, oldest first.
export const liveTasks = (projectId) => [...(live.get(projectId)?.values() || [])];

export const short = (text, n = 70) => {
  const s = String(text || '').replace(/\s+/g, ' ').trim();
  return s.length > n ? `${s.slice(0, n - 1)}…` : s;
};

const clip = (text) => {
  const s = String(text || '');
  return s.length > MAX_DETAIL ? `${s.slice(0, MAX_DETAIL - 1)}…` : s;
};

export class Activity {
  // parent: the step that the steps tracked here are part of (Opus's searches while it plans).
  constructor(store, projectId, parent = null) {
    this.store = store;
    this.projectId = projectId;
    this.parent = parent;
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

  // Run `work(step)` as a step of `agent`; `summarize(result)` says how it ended. `step.meter(sink)`
  // counts the tokens of the model calls made with that sink, and `step.activity` tracks steps that
  // are part of this one.
  async track({ agent, step, text, paperId = null }, work, summarize = null) {
    const id = nextId++;
    if (!live.has(this.projectId)) live.set(this.projectId, new Map());
    const tasks = live.get(this.projectId);
    const task = { id, agent, step, text, started: Date.now(), parent: this.parent, usage: null, models: new Set() };
    tasks.set(id, task);
    this.liveChanged();
    const handle = {
      activity: new Activity(this.store, this.projectId, id),
      meter: (sink) => (callStep, model, usage) => {
        const u = (task.usage ||= { input: 0, output: 0, calls: 0 });
        u.input += (usage.input_tokens || 0) + (usage.cache_read_input_tokens || 0) + (usage.cache_creation_input_tokens || 0);
        u.output += usage.output_tokens || 0;
        u.calls += 1;
        if (model) task.models.add(model);
        this.liveChanged();
        return sink?.(callStep, model, usage);
      },
    };
    const started = performance.now();
    const entry = (phase, detail) => ({
      agent, step, phase, text, detail: clip(detail), ms: Math.round(performance.now() - started), paper_id: paperId,
      usage: task.usage, models: [...task.models], job: jobId(id), parent: jobId(this.parent),
      started: new Date(task.started).toISOString(),
    });
    try {
      const result = await work(handle);
      let detail = '';
      try {
        detail = summarize ? summarize(result) : '';
      } catch { /* a summary is optional */ }
      await this.log(entry('done', detail));
      return result;
    } catch (error) {
      await this.log(entry('error', describeError(error)));
      throw error;
    } finally {
      tasks.delete(id);
      this.liveChanged();
    }
  }

  note({ agent, step, text, detail = '', paperId = null, phase = 'note' }) {
    return this.log({ agent, step, phase, text, detail: clip(detail), ms: null, paper_id: paperId, job: jobId(nextId++), parent: jobId(this.parent) });
  }
}

// For callers that don't keep a log.
const NO_STEP = { meter: (sink) => sink, get activity() { return NO_ACTIVITY; } };
export const NO_ACTIVITY = { track: (_, work) => work(NO_STEP), note: async () => {} };
