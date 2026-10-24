// The search-and-extract pipeline, running in the page:
//
//   spec (manager) -> search plan (manager + tools) -> run the levels from the top down -> screen (workers)
//   -> add what the included reviews cite -> screen those (workers)
//   -> full text (browser, or Claude's fetcher when the browser may not) -> extract (workers)
//   -> verify (code) -> adjudicate escalations (manager) -> review (you)
//
// A port of python/esf/pipeline.py. Everything is stored in IndexedDB as it happens, so a run that
// is cut short (the tab closed) can be continued.

import { NO_ACTIVITY, short } from './activity.js';
import { describeError, ModelDeclined, ModelTruncated } from './claude.js';
import { buildEvidence, decisionsBasis, overlapRecords } from './evidence.js';
import { LEVEL_NAMES, LEVELS, PROMPTS } from './generated.js';
import { jatsToText } from './jats.js';
import { pdfText } from './pdf.js';
import {
  bytesToBase64, europepmcPmid, europepmcRecord, europepmcUrl, SourceError, trialRecord, trialResultsText,
} from './sources.js';
import { escalationReasons, inAuditSample, verify } from './verify.js';

export const DEFAULTS = {
  auditFraction: 0.1, perQueryLimit: 100, maxCandidates: 300, maxReviewsFollowed: 10, maxCited: 150,
  maxExtractions: 60, concurrency: 6, searchMaxIterations: 25,
};

const NCT = /^NCT\d{8}$/i;
// Publication types that are not studies; a review's reference list is full of them.
const NOT_STUDIES = new Set([
  'Review', 'Systematic Review', 'Meta-Analysis', 'Editorial', 'Comment', 'Letter', 'News',
  'Guideline', 'Practice Guideline', 'Published Erratum', 'Retraction of Publication',
]);
const TEXT_KIND = { registry: 'registry results', pmc_xml: 'PMC full text', oa_pdf: 'open-access PDF', abstract: 'abstract only', upload: 'your PDF' };
const countValues = (extraction) => extraction.results.reduce((n, r) => n + r.values.length, 0);
const STUDY_TYPES = {
  rct: 'trial', systematic_review_or_meta_analysis: 'review', observational: 'observational', pilot_or_feasibility: 'pilot',
  protocol_without_results: 'protocol', other: 'other',
};
// What an extraction found, numbers included: "2 results, 8 values. PHQ-9 at 12 weeks: App mean 8.1, …".
const found = (x) => [
  `${plural(x.results.length, 'result')}, ${plural(countValues(x), 'value')}.`,
  ...x.results.map((r) => `${r.outcome_measure}${r.timepoint ? ` at ${r.timepoint}` : ''}: `
    + `${r.values.map((v) => `${v.group} ${v.statistic.replaceAll('_', ' ')} ${v.value}`).join(', ')}`),
].join('\n');
const plural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;
const PROCESSED = new Set(['extracted', 'needs_manual', 'no_fulltext', 'error']);

// Errors after which every later model call fails the same way (a refused key, a model the key may
// not use, no credit left): the run stops and says why, instead of sending a request for every
// remaining record and paper. Mirrors stops_the_run in python/esf/pipeline.py.
const STOPPING_CODES = new Set(['invalid_api_key', 'insufficient_quota', 'model_not_found']);
export function stopsTheRun(error) {
  if (error?.name === 'OpenAIError') return STOPPING_CODES.has(error.code) || [401, 403].includes(error.status);
  return [401, 403, 404].includes(error?.status); // the Anthropic SDK's errors carry the HTTP status
}

async function pool(items, limit, fn, onEach) {
  let next = 0;
  const workers = Array.from({ length: Math.min(limit, items.length) }, async () => {
    while (next < items.length) {
      const item = items[next++];
      await fn(item);
      await onEach?.();
    }
  });
  await Promise.all(workers);
}

function recordText(paper) {
  return [
    `${paper.source === 'clinicaltrials' ? 'Registry record' : 'Article'}: ${paper.title}`,
    `Year: ${paper.year || 'unknown'}; source: ${paper.journal || ''}`,
    `Publication types: ${(paper.pubtypes || []).join(', ') || 'unknown'}`,
    ...((paper.found_in || []).includes('full_text') ? [PROMPTS.FOUND_BY_FULL_TEXT] : []),
    '',
    paper.abstract || '(no abstract available)',
  ].join('\n');
}

export function documentBlock(doc, title) {
  const name = (title || 'Document').slice(0, 500);
  if (doc.pdf) return { type: 'document', source: { type: 'base64', media_type: 'application/pdf', data: doc.pdf }, title: name };
  return { type: 'document', source: { type: 'text', media_type: 'text/plain', data: doc.text }, title: name };
}

// The finished summary to keep while another is written: the summary itself if it is done, or the one
// a running summary kept. Mirrors previous_summary in python/esf/pipeline.py.
export function previousSummary(summary) {
  if (summary?.status === 'done') {
    const { last_error: _, ...kept } = summary;
    return kept;
  }
  return summary?.previous ?? null;
}

export class Pipeline {
  constructor({ store, llm, sources, settings = {}, activity = NO_ACTIVITY }) {
    this.store = store;
    this.llm = llm; // Claude (claude.js) or OpenAI (openai.js)
    this.sources = sources;
    this.settings = { ...DEFAULTS, ...settings };
    this.activity = activity;
  }

  sink(projectId) {
    return (step, model, usage) => this.store.addUsage(projectId, step, model, usage);
  }

  progress(projectId, stage, done = 0, total = 0) {
    return this.store.updateProject(projectId, { progress: { stage, done, total } });
  }

  // -- step 1: read the problem ---------------------------------------------------

  async readSpec(projectId) {
    const project = await this.store.project(projectId);
    await this.store.updateProject(projectId, { status: 'reading', error: null });
    try {
      const spec = await this.activity.track(
        { agent: 'manager', step: 'spec', text: 'Reading the problem' },
        (step) => this.llm.readSpec(project.paragraph, { sink: step.meter(this.sink(projectId)) }),
        (s) => `${s.design.replaceAll('_', ' ')}; outcomes: ${s.outcomes.map((o) => o.measure || o.name).join(', ')}; `
          + `${plural(s.statistics_needed.length, 'number')} to find; ${plural(s.open_questions.length, 'question')} worth deciding`,
      );
      await this.store.updateProject(projectId, { status: 'spec_ready', spec });
    } catch (error) {
      await this.store.updateProject(projectId, { status: 'error', error: `Reading the problem failed. ${describeError(error)}` });
    }
  }

  // -- step 2: search, screen, extract (or continue where a run stopped) ----------

  async run(projectId, { resume = false } = {}) {
    let project = await this.store.project(projectId);
    const spec = project.spec;
    if (!resume) {
      await this.store.clearPapers(projectId);
      await this.store.updateProject(projectId, {
        error: null, search_plan: null, search_log: null, collected: false, followed: false, overlap: null, summary: null, progress: null,
      });
      project = await this.store.project(projectId);
    } else {
      await this.store.updateProject(projectId, { error: null });
    }
    await this.activity.note({ agent: 'code', step: 'run', text: resume ? 'Continuing the search' : 'Starting a new search' });
    try {
      let plan = project.search_plan;
      if (!plan) {
        await this.store.updateProject(projectId, { status: 'searching' });
        await this.progress(projectId, 'planning the search');
        plan = await this.activity.track(
          { agent: 'manager', step: 'search', text: 'Planning the search' },
          // The manager's searches while it plans show as part of this step.
          (step) => this.llm.planSearch(spec, new Toolbox(this.sources, step.activity), {
            sink: step.meter(this.sink(projectId)), maxIterations: this.settings.searchMaxIterations,
          }),
          (p) => [
            `Final plan: ${p.levels.map((l) => l.level).join(' → ') || 'no PubMed levels'}; `
              + `${plural(p.clinicaltrials_queries.length, 'ClinicalTrials.gov query', 'ClinicalTrials.gov queries')}`,
            ...p.levels.map((l) => `${LEVEL_NAMES[l.level]}: ${l.query}`),
            ...p.clinicaltrials_queries.map((q) => `${LEVEL_NAMES.registry}: ${q}`),
          ].join('\n'),
        );
        await this.store.updateProject(projectId, { search_plan: plan });
      }
      if (!project.collected) {
        await this.store.updateProject(projectId, { status: 'searching' });
        await this.collect(projectId, plan);
        await this.store.updateProject(projectId, { collected: true });
      }
      await this.store.updateProject(projectId, { status: 'screening' });
      await this.screenAll(projectId, spec);
      if (!(await this.store.project(projectId)).followed) {
        await this.followReviews(projectId);
        await this.store.updateProject(projectId, { followed: true });
      }
      await this.screenAll(projectId, spec);
      await this.store.updateProject(projectId, { status: 'extracting' });
      await this.extractAll(projectId, spec);
      try {
        await this.progress(projectId, 'checking for overlap');
        await this.checkOverlap(projectId);
      } catch (error) { // the extractions stand without it; the summary tries again
        await this.store.updateProject(projectId, { overlap: { error: describeError(error) } });
      }
    } catch (error) {
      await this.store.updateProject(projectId, { status: 'error', error: describeError(error) });
      await this.activity.note({ agent: 'code', step: 'run', phase: 'error', text: 'The search stopped', detail: describeError(error) });
      return;
    }
    await this.store.updateProject(projectId, { status: 'done', progress: { stage: 'done', done: 0, total: 0 } });
    const counts = await this.store.statusCounts(projectId);
    await this.activity.note({ agent: 'code', step: 'run', text: 'Search finished', detail: `${counts.extracted || 0} extracted; ready for review` });
  }

  // Run the levels from the top down, then the registry queries, and store the candidates in that
  // order. Each search's hits and how many records it added go to the search log.
  async collect(projectId, plan) {
    const limit = this.settings.perQueryLimit;
    // A search planned before the levels existed, continued now: its queries count as one level.
    const planned = plan.levels || (plan.pubmed_queries || []).map((query) => ({ level: 'primary', query, why: '' }));
    const levels = planned.filter((l) => l.query.trim()).sort((a, b) => LEVELS.indexOf(a.level) - LEVELS.indexOf(b.level));
    const queries = [
      ...levels.map((l) => [l.level === 'full_text' ? 'europepmc' : 'pubmed', l.level, l.query]),
      ...plan.clinicaltrials_queries.map((q) => ['clinicaltrials', 'registry', q]),
    ];
    // Where each record was found, in the order first found; `first` is the index of the search that
    // found it first. Papers are keyed "pmid:<PMID>", or by their Europe PMC ID when not in PubMed
    // (`record` then holds them, as PubMed can't supply them); registry records by NCT ID.
    const papers = new Map();
    const trials = new Map();
    const found = (table, key, level, label, index, record = null) => {
      const item = table.get(key) || { foundBy: [], foundIn: [], first: index, record };
      item.foundBy.push(label);
      if (!item.foundIn.includes(level)) item.foundIn.push(level);
      table.set(key, item);
    };
    const entries = [];
    const failed = [];
    for (const [index, [source, level, query]] of queries.entries()) {
      await this.progress(projectId, 'running the search', index, queries.length);
      const label = `${LEVEL_NAMES[level]}: ${query}`;
      const entry = { level, source, query };
      try {
        await this.activity.track({ agent: 'code', step: 'collect', text: `${LEVEL_NAMES[level]}: ${short(query, 90)}` }, async () => {
          if (source === 'pubmed') {
            const [hits, ids] = await this.sources.pubmed.search(query, limit);
            for (const id of ids) found(papers, `pmid:${id}`, level, label, index);
            Object.assign(entry, { hits, taken: ids.length });
          } else if (source === 'europepmc') {
            const [hits, results] = await this.sources.europepmc.search(query, limit);
            for (const result of results) {
              const pmid = europepmcPmid(result);
              if (pmid) found(papers, `pmid:${pmid}`, level, label, index);
              else found(papers, String(result.id), level, label, index, europepmcRecord(result));
            }
            Object.assign(entry, { hits, taken: results.length });
          } else {
            const [hits, studies] = await this.sources.trials.search(query, limit, true);
            for (const record of studies.map(trialRecord)) found(trials, record.ext_id, level, label, index, record);
            Object.assign(entry, { hits, taken: studies.length });
          }
        }, () => `${plural(entry.hits, 'hit')}; the top ${entry.taken} taken`);
      } catch (error) {
        failed.push(`${label} (${describeError(error)})`);
        entry.error = describeError(error);
      }
      entries.push(entry);
      // Saved as each search returns, so the page can show the levels filling in.
      await this.store.updateProject(projectId, { search_log: entries.map((e) => ({ ...e })) });
    }
    if (failed.length && failed.length === queries.length) throw new Error(`Every search query failed: ${failed.join('; ')}`);
    if (failed.length) await this.store.updateProject(projectId, { search_plan: { ...plan, failed_queries: failed } });

    const named = LEVEL_NAMES.named;
    for (const extra of plan.extra_ids.map((x) => x.trim())) {
      if (NCT.test(extra)) {
        const study = await this.sources.trials.study(extra.toUpperCase()).catch(() => null);
        if (study) found(trials, extra.toUpperCase(), 'named', named, entries.length, trialRecord(study));
      } else if (/^\d+$/.test(extra)) {
        found(papers, `pmid:${extra}`, 'named', named, entries.length);
      }
    }
    if ([...papers.values(), ...trials.values()].some((item) => item.first === entries.length)) {
      entries.push({ level: 'named', source: 'pubmed', query: plan.extra_ids.join(', '), hits: null, taken: null });
    }

    const keys = [...papers.keys()].slice(0, this.settings.maxCandidates);
    const pmidList = keys.filter((key) => key.startsWith('pmid:')).map((key) => key.slice('pmid:'.length));
    const records = pmidList.length ? await this.sources.pubmed.fetch(pmidList) : [];
    const byPmid = new Map(records.map((r) => [`pmid:${r.pmid}`, r]));
    const articles = keys.map((key) => [byPmid.get(key) || papers.get(key).record, papers.get(key)]).filter(([record]) => record);
    const registry = [...trials.values()].map((item) => [item.record, item]);
    // Interleave the two sources so the cap on candidates keeps the top of each.
    const merged = [];
    for (let i = 0; i < Math.max(articles.length, registry.length); i++) {
      if (articles[i]) merged.push(articles[i]);
      if (registry[i]) merged.push(registry[i]);
    }
    let added = 0;
    const bySource = { pubmed: 0, europepmc: 0, clinicaltrials: 0 };
    for (const [record, item] of merged) {
      if (added >= this.settings.maxCandidates) break;
      if (await this.store.addPaper(projectId, { ...record, found_by: item.foundBy, found_in: item.foundIn, status: 'candidate' })) {
        added += 1;
        bySource[record.source] += 1;
        entries[item.first].added = (entries[item.first].added || 0) + 1;
      }
    }
    for (const entry of entries) if (!entry.error) entry.added ||= 0;
    await this.store.updateProject(projectId, { search_log: entries });
    await this.activity.note({
      agent: 'code', step: 'collect', text: `Collected ${plural(added, 'record')} to screen`,
      detail: `${plural(bySource.pubmed, 'paper')} from PubMed, ${bySource.europepmc} only in Europe PMC, `
        + `${plural(bySource.clinicaltrials, 'registry record')} from ClinicalTrials.gov`,
    });
    await this.progress(projectId, 'running the search', queries.length, queries.length);
  }

  // The step down from the reviews: add the studies cited by the reviews that passed screening.
  async followReviews(projectId) {
    const papers = await this.store.papers(projectId);
    const reviews = papers.filter((p) => p.source === 'pubmed' && p.status === 'screened_in'
      && p.screen?.study_type === 'systematic_review_or_meta_analysis').slice(0, this.settings.maxReviewsFollowed);
    const entry = { level: 'cited', source: 'pubmed', reviews: reviews.length, cited: 0, already: 0, added: 0 };
    const known = new Map(papers.filter((p) => p.pmid).map((p) => [p.pmid, p]));
    const cited = new Map();
    // Which reviews cite each record, new or already found: the overlap check needs to know.
    const citing = new Map();
    const failed = [];
    if (reviews.length) {
      await this.activity.note({ agent: 'code', step: 'follow', text: `Following the references of ${plural(reviews.length, 'included review')}` });
    }
    for (const [i, review] of reviews.entries()) {
      await this.progress(projectId, "reading the reviews' references", i, reviews.length);
      let refs;
      try {
        refs = await this.activity.track(
          { agent: 'code', step: 'follow', text: `References of “${short(review.title)}”`, paperId: review.id },
          () => this.sources.references(review.pmid),
          (r) => `${plural(r.length, 'cited record')} in PubMed`,
        );
      } catch (error) {
        failed.push(`PMID ${review.pmid} (${describeError(error)})`);
        continue;
      }
      entry.cited += refs.length;
      const label = `${LEVEL_NAMES.cited}: ${review.title} (PMID ${review.pmid})`;
      for (const pmid of refs) {
        citing.set(pmid, [...(citing.get(pmid) || []), review.pmid]);
        if (known.has(pmid)) entry.already += 1;
        else cited.set(pmid, [...(cited.get(pmid) || []), label]);
      }
    }
    for (const [pmid, reviewsCiting] of citing) {
      const paper = known.get(pmid);
      if (paper) await this.store.updatePaper(paper.id, { cited_by: [...new Set([...(paper.cited_by || []), ...reviewsCiting])].sort() });
    }
    // Fetch more than the cap: reference lists are full of reviews, guidelines and methods papers.
    const ids = [...cited.keys()].slice(0, this.settings.maxCited * 3);
    let fetched = [];
    try {
      if (ids.length) fetched = await this.sources.pubmed.fetch(ids);
    } catch (error) {
      failed.push(`fetching the ${ids.length} cited records (${describeError(error)})`);
    }
    const records = fetched.filter((r) => !r.pubtypes.some((t) => NOT_STUDIES.has(t)));
    for (const record of records) {
      if (entry.added >= this.settings.maxCited) break;
      if (await this.store.addPaper(projectId, {
        ...record, found_by: cited.get(record.pmid), found_in: ['cited'], cited_by: citing.get(record.pmid), status: 'candidate',
      })) {
        entry.added += 1;
      }
    }
    entry.not_studies = fetched.length - records.length;
    if (failed.length) entry.failed = failed;
    const log = (await this.store.project(projectId)).search_log || [];
    await this.store.updateProject(projectId, { search_log: [...log.filter((e) => e.level !== 'cited'), entry] });
    if (reviews.length) {
      await this.activity.note({
        agent: 'code', step: 'follow', text: `Added ${plural(entry.added, 'study', 'studies')} cited by the reviews`,
        detail: `${entry.cited} cited in PubMed; ${entry.already} already found; ${entry.not_studies} not studies (reviews, guidelines, letters)`,
      });
    }
    await this.progress(projectId, "reading the reviews' references", reviews.length, reviews.length);
  }

  async screenAll(projectId, spec) {
    const papers = await this.store.papers(projectId, 'candidate');
    const sink = this.sink(projectId);
    let done = 0;
    await this.progress(projectId, 'screening', 0, papers.length);
    const stop = { error: null };
    await pool(papers, this.settings.concurrency, async (paper) => {
      if (stop.error) return; // left as a candidate: the run stops, and Continue screens it
      try {
        const decision = await this.activity.track(
          { agent: 'worker', step: 'screen', text: `Screening “${short(paper.title)}”`, paperId: paper.id },
          (step) => this.llm.screen(spec, recordText(paper), { sink: step.meter(sink) }),
          (d) => `${d.decision} (${STUDY_TYPES[d.study_type] || d.study_type}): ${d.reason}`,
        );
        const status = { include: 'screened_in', exclude: 'screened_out', unsure: 'unsure' }[decision.decision];
        await this.store.updatePaper(paper.id, { status, screen: decision });
      } catch (error) {
        if (stopsTheRun(error)) {
          stop.error ||= error;
          return;
        }
        // Never drop a record silently: an unscreened record goes forward as "unsure".
        await this.store.updatePaper(paper.id, {
          status: 'unsure', screen: { decision: 'unsure', reason: `Screening failed (${describeError(error)}); review by hand.` },
        });
      }
    }, () => this.progress(projectId, 'screening', ++done, papers.length));
    if (stop.error) throw stop.error;
  }

  async extractAll(projectId, spec) {
    const all = await this.store.papers(projectId);
    const room = Math.max(0, this.settings.maxExtractions - all.filter((p) => PROCESSED.has(p.status)).length);
    const waiting = [...all.filter((p) => p.status === 'screened_in'), ...all.filter((p) => p.status === 'unsure')];
    const queue = waiting.slice(0, room);
    for (const paper of waiting.slice(room)) {
      await this.store.updatePaper(paper.id, { error: 'Not extracted: over the per-search limit; use Extract.' });
    }
    let done = 0;
    await this.progress(projectId, 'extracting', 0, queue.length);
    const stop = { error: null };
    await pool(queue, this.settings.concurrency, (paper) => (stop.error ? null : this.extractPaper(projectId, spec, paper.id, { stop })),
      () => this.progress(projectId, 'extracting', ++done, queue.length));
    if (stop.error) throw stop.error;
  }

  // -- across papers: overlap and the summary ---------------------------------------

  // The manager looks across the extracted records for reports that would be counted twice.
  async checkOverlap(projectId) {
    const project = await this.store.project(projectId);
    const records = overlapRecords(await this.store.papers(projectId));
    let report = { groups: [], notes: '' };
    if (records.length > 1) {
      report = await this.activity.track(
        { agent: 'manager', step: 'overlap', text: `Checking ${plural(records.length, 'record')} for overlap` },
        (step) => this.llm.checkOverlap(project.spec, records, { sink: step.meter(this.sink(projectId)) }),
        (r) => [
          r.groups.length ? plural(r.groups.length, 'overlap') : 'No overlap found',
          ...r.groups.map((g) => `${g.records.join(', ')}: ${g.relation.replaceAll('_', ' ')}${g.use ? `; use ${g.use}` : ''}`),
        ].join('\n'),
      );
    }
    await this.store.updateProject(projectId, { overlap: { report, records: records.map((r) => r.key), input: records } });
  }

  // The manager describes, for each effect size the plan needs, the values the investigator kept: how they
  // compare and where they share participants. It does not combine them or recommend one.
  async summarize(projectId) {
    const previous = previousSummary((await this.store.project(projectId)).summary);
    await this.store.updateProject(projectId, { summary: { status: 'running', previous } });
    try {
      const papers = await this.store.papers(projectId);
      let overlap = (await this.store.project(projectId)).overlap || {};
      if (!overlap.report || JSON.stringify(overlap.input) !== JSON.stringify(overlapRecords(papers))) {
        await this.checkOverlap(projectId);
        overlap = (await this.store.project(projectId)).overlap;
      }
      const project = await this.store.project(projectId);
      const decisions = await this.store.decisions(projectId);
      const evidence = buildEvidence(papers, decisions, overlap.report);
      if (!evidence.values.length) throw new Error('There is nothing to summarize: no extracted result is kept.');
      const result = await this.activity.track(
        {
          agent: 'manager', step: 'summarize',
          text: `Summarizing ${plural(evidence.values.length, 'value')} from ${plural(evidence.records.length, 'record')}`,
        },
        (step) => this.llm.summarize(project.spec, overlap.report, evidence, { sink: step.meter(this.sink(projectId)) }),
        (r) => {
          // Keys shown with their values, as on the page: "SDs of 4.2 [V4] to 4.5 [V8]".
          const values = new Map(evidence.values.map((v) => [v.key, v.value]));
          const shown = (text) => text.replace(/\[(V\d+)\]/g, (key, k) => (values.has(k) ? `${values.get(k)} ${key}` : key));
          return [shown(r.overview), ...r.needs.map((n) => `${n.need}: ${plural(n.values.length, 'value')}`)].join('\n');
        },
      );
      await this.store.updateProject(projectId, {
        summary: { status: 'done', result, evidence, basis: decisionsBasis(papers, decisions) },
      });
    } catch (error) {
      const message = describeError(error);
      await this.store.updateProject(projectId, {
        summary: previous ? { ...previous, last_error: message } : { status: 'error', error: message },
      });
    }
  }

  // -- one paper ------------------------------------------------------------------

  // A paper's authors with their affiliations. A record stored before they were kept gets them from
  // its source now (PubMed, Europe PMC or ClinicalTrials.gov); an empty list if it can't.
  async authorList(paperId) {
    const paper = await this.store.paper(paperId);
    if (paper.author_list?.length) return paper.author_list;
    let record = null;
    try {
      if (paper.source === 'clinicaltrials') {
        const study = await this.sources.trials.study(paper.ext_id);
        record = study ? trialRecord(study) : null;
      } else if (paper.pmid) {
        record = (await this.sources.pubmed.fetch([paper.pmid]))[0] || null;
      } else {
        const kind = paper.ext_id.startsWith('PPR') ? 'PPR' : 'PMC';
        const [, results] = await this.sources.europepmc.search(`EXT_ID:${paper.ext_id} AND SRC:${kind}`, 5);
        const found = results.find((r) => String(r.id) === paper.ext_id);
        record = found ? europepmcRecord(found) : null;
      }
    } catch (error) {
      console.warn('Could not fetch the authors:', error);
    }
    if (!record?.author_list?.length) return [];
    await this.store.updatePaper(paperId, { author_list: record.author_list, authors: paper.authors || record.authors });
    return record.author_list;
  }

  // -- one paper: text and extraction -----------------------------------------------

  async fullText(paper) {
    const sources = this.sources;
    if (paper.source === 'clinicaltrials') {
      const study = await sources.trials.study(paper.ext_id);
      return study ? { kind: 'registry', text: trialResultsText(study), url: `https://clinicaltrials.gov/study/${paper.ext_id}` } : null;
    }
    if (paper.pmcid) {
      const xml = await sources.europepmc.fulltextXml(paper.pmcid);
      if (xml) {
        const text = jatsToText(xml);
        // Some records hold only front matter; those fall through to the next source.
        if (text.length > 500) return { kind: 'pmc_xml', text, url: `https://pmc.ncbi.nlm.nih.gov/articles/${paper.pmcid}/` };
      }
    }
    let missed = null; // why an open-access PDF was not read
    if (paper.doi) {
      const url = await sources.unpaywall.pdfUrl(paper.doi);
      if (url) {
        let bytes = null;
        try {
          bytes = await sources.unpaywall.downloadPdf(url);
        } catch (error) {
          // A PDF the page may not download, with no model to fetch it (OpenAI) or a fetch that failed:
          // the abstract is still read, as in the local edition, and the PDF can be uploaded.
          if (!(error instanceof SourceError) || !paper.abstract) throw error;
          missed = `The open-access PDF could not be downloaded (${error.message.replace(/\.$/, '')}), so the abstract was read. Upload the PDF to read the full text.`;
        }
        if (bytes) {
          return {
            kind: 'oa_pdf', text: await pdfText(bytes), url, pdf: bytesToBase64(bytes),
            fetched_by: sources.http.isBlocked(url) ? 'fetcher' : 'browser',
          };
        }
      }
    }
    if (paper.abstract) {
      const url = paper.source === 'europepmc' ? europepmcUrl(paper.ext_id) : `https://pubmed.ncbi.nlm.nih.gov/${paper.ext_id}/`;
      return { kind: 'abstract', text: `# ${paper.title}\n\n## Abstract\n${paper.abstract}\n`, url, ...(missed ? { note: missed } : {}) };
    }
    return null;
  }

  // upload: the bytes of a PDF the user supplied, used instead of searching for the text.
  // stop: in a run, where an error that would fail every paper stops the run.
  async extractPaper(projectId, spec, paperId, { upload = null, uploadName = '', stop = null } = {}) {
    try {
      await this.extractOne(projectId, spec, paperId, upload, uploadName);
    } catch (error) {
      await this.store.updatePaper(paperId, { status: 'error', error: `Extraction failed. ${describeError(error)}` });
      if (stop && stopsTheRun(error)) stop.error ||= error;
    }
  }

  async extractOne(projectId, spec, paperId, upload, uploadName) {
    const paper = await this.store.paper(paperId);
    const sink = this.sink(projectId);
    const title = short(paper.title || paper.ext_id);
    await this.store.updatePaper(paperId, { status: 'fetching', error: null });
    let doc;
    if (upload) {
      doc = { kind: 'upload', text: await pdfText(upload), url: uploadName, pdf: bytesToBase64(upload) };
      await this.activity.note({ agent: 'code', step: 'fetch', text: `Read your PDF for “${title}”`, paperId });
    } else {
      try {
        doc = await this.activity.track(
          { agent: 'code', step: 'fetch', text: `Getting the text of “${title}”`, paperId },
          () => this.fullText(paper),
          (d) => (d ? `${TEXT_KIND[d.kind]}${d.fetched_by === 'fetcher' ? ', fetched by Claude' : ''}, `
            + `${Math.max(1, Math.round(d.text.length / 1000))}k characters${d.note ? `\n${d.note}` : ''}` : 'no open text found'),
        );
      } catch (error) {
        await this.store.updatePaper(paperId, { status: 'no_fulltext', error: `Could not get the text: ${describeError(error)}` });
        return;
      }
    }
    if (!doc) {
      await this.store.updatePaper(paperId, { status: 'no_fulltext', error: 'No open full text or abstract found. Upload the PDF if you have it.' });
      return;
    }
    await this.store.saveDocument(projectId, paperId, doc);
    await this.store.updatePaper(paperId, {
      status: 'extracting', fulltext_kind: doc.kind, fulltext_url: doc.url, fetched_by: doc.fetched_by || null, fulltext_note: doc.note || null,
    });

    const document = documentBlock(doc, paper.title || paper.ext_id);
    const quotesReliable = !doc.pdf && Boolean(doc.text.trim());
    let worker = null;
    let report = null;
    let workerError = null;
    try {
      worker = await this.activity.track(
        { agent: 'worker', step: 'extract', text: `Extracting “${title}”`, paperId },
        (step) => this.llm.extract(spec, document, { sink: step.meter(sink) }),
        found,
      );
      report = verify(worker, doc.text, quotesReliable);
    } catch (error) {
      if (stopsTheRun(error)) throw error;
      if (error instanceof ModelDeclined) workerError = `the worker ${error.message}`;
      else if (error instanceof ModelTruncated) workerError = "the worker's extraction was cut off before it finished";
      else workerError = `the worker failed: ${describeError(error)}`;
    }
    let reasons = workerError ? [workerError] : escalationReasons(worker, report);
    const audited = !reasons.length && await inAuditSample(`${projectId}:${paperId}`, this.settings.auditFraction);
    if (audited) reasons = ['random audit of an extraction that passed every check'];
    if (worker) {
      await this.activity.note({
        agent: 'code', step: 'verify', paperId, phase: reasons.length ? 'flag' : 'note',
        text: `Checked “${title}”`,
        detail: audited ? 'every check passed; picked for the random audit, so the manager reviews it'
          : reasons.length ? `${plural(reasons.length, 'flag')}, so the manager reviews it: ${short(reasons[0], 120)}`
            : 'every quote found and every number plausible',
      });
    }

    let final = worker;
    let finalReport = report;
    let extractedBy = 'worker';
    if (reasons.length) {
      try {
        final = await this.activity.track(
          { agent: 'manager', step: 'adjudicate', text: `Reviewing “${title}”`, paperId },
          (step) => this.llm.adjudicate(spec, document, worker, reasons, { sink: step.meter(sink) }),
          found,
        );
        finalReport = verify(final, doc.text, quotesReliable);
        extractedBy = 'manager';
      } catch (error) {
        if (stopsTheRun(error)) throw error;
        if (!worker) {
          await this.store.updatePaper(paperId, {
            status: 'needs_manual', escalation: { reasons, audit: audited },
            error: `Neither model completed the extraction. ${describeError(error)}`,
          });
          return;
        }
        reasons.push(`manager review failed: ${describeError(error)}`);
      }
    }
    await this.store.updatePaper(paperId, {
      status: 'extracted', extraction: final, extracted_by: extractedBy, extracted_model: this.llm.models?.[extractedBy] || null,
      verification: finalReport,
      escalation: reasons.length ? { reasons, audit: audited } : null,
    });
  }
}

// What the search agent can call while it explores. Each call shows in the activity log as the manager's.
export class Toolbox {
  constructor(sources, activity = NO_ACTIVITY) {
    this.sources = sources;
    this.activity = activity;
  }

  countPubmed(queries) {
    return this.activity.track({ agent: 'manager', step: 'search', text: `Counts ${plural(queries.length, 'PubMed query', 'PubMed queries')}` }, async () => {
      const lines = [];
      for (const [i, query] of queries.entries()) {
        let hits;
        try {
          hits = (await this.sources.pubmed.search(query, 0))[0].toLocaleString('en-US');
        } catch (error) {
          hits = `error: ${describeError(error)}`;
        }
        lines.push(`#${i + 1} ${hits} | ${query}`);
      }
      return lines.join('\n');
    }, (out) => out.split('\n').map((line) => line.split(' | ')[0]).join(', '));
  }

  previewPubmed(query, maxResults) {
    return this.activity.track({ agent: 'manager', step: 'search', text: `Tries PubMed: ${short(query, 90)}` }, async () => {
      const [count, ids] = await this.sources.pubmed.search(query, maxResults);
      const lines = [`Total hits: ${count}`];
      for (const r of await this.sources.pubmed.summaries(ids)) lines.push(`${r.pmid} | ${r.year} | ${r.pubtypes.join(', ')} | ${r.title}`);
      return lines.join('\n');
    }, (out) => out.split('\n')[0].replace('Total hits:', 'hits:'));
  }

  previewEuropepmc(query, maxResults) {
    return this.activity.track({ agent: 'manager', step: 'search', text: `Tries Europe PMC: ${short(query, 90)}` }, async () => {
      const [total, results] = await this.sources.europepmc.search(query, maxResults, { abstracts: false });
      const lines = [`Total hits: ${total}`];
      for (const r of results) {
        const types = r.pubType || (r.pubTypeList?.pubType || []).join(', ');
        lines.push(`${r.id} | ${r.pubYear ?? ''} | ${r.source} | ${types} | ${r.title ?? ''}`);
      }
      return lines.join('\n');
    }, (out) => out.split('\n')[0].replace('Total hits:', 'hits:'));
  }

  previewTrials(query, maxResults) {
    return this.activity.track({ agent: 'manager', step: 'search', text: `Tries ClinicalTrials.gov: ${short(query, 90)}` },
      () => this.trialsPreview(query, maxResults), (out) => out.split('\n')[0]);
  }

  async trialsPreview(query, maxResults) {
    const [total, studies] = await this.sources.trials.search(query, maxResults, true);
    const lines = [`Total studies with posted results: ${total}`];
    for (const study of studies) {
      const r = trialRecord(study);
      lines.push(`${r.ext_id} | ${study.protocolSection?.statusModule?.overallStatus || ''} | ${r.year} | ${r.title}`);
    }
    return lines.join('\n');
  }
}
