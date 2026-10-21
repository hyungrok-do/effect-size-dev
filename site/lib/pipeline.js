// The search-and-extract pipeline, running in the page:
//
//   spec (Opus) -> search plan (Opus + tools) -> run queries -> screen (Haiku)
//   -> full text (browser, or Sonnet when the browser may not) -> extract (Haiku)
//   -> verify (code) -> adjudicate escalations (Opus) -> review (you)
//
// A port of python/esf/pipeline.py. Everything is stored in IndexedDB as it happens, so a run that
// is cut short (the tab closed) can be continued.

import { NO_ACTIVITY, short } from './activity.js';
import { describeError, ModelDeclined, ModelTruncated } from './claude.js';
import { jatsToText } from './jats.js';
import { pdfText } from './pdf.js';
import { bytesToBase64, trialRecord, trialResultsText } from './sources.js';
import { escalationReasons, inAuditSample, verify } from './verify.js';

export const DEFAULTS = {
  auditFraction: 0.1, perQueryLimit: 100, maxCandidates: 300, maxExtractions: 60, concurrency: 6, searchMaxIterations: 25,
};

const NCT = /^NCT\d{8}$/i;
const TEXT_KIND = { registry: 'registry results', pmc_xml: 'PMC full text', oa_pdf: 'open-access PDF', abstract: 'abstract only', upload: 'your PDF' };
const countValues = (extraction) => extraction.results.reduce((n, r) => n + r.values.length, 0);
const plural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;
const PROCESSED = new Set(['extracted', 'needs_manual', 'no_fulltext', 'error']);

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
    '',
    paper.abstract || '(no abstract available)',
  ].join('\n');
}

export function documentBlock(doc, title) {
  const name = (title || 'Document').slice(0, 500);
  if (doc.pdf) return { type: 'document', source: { type: 'base64', media_type: 'application/pdf', data: doc.pdf }, title: name };
  return { type: 'document', source: { type: 'text', media_type: 'text/plain', data: doc.text }, title: name };
}

export class Pipeline {
  constructor({ store, claude, sources, settings = {}, activity = NO_ACTIVITY }) {
    this.store = store;
    this.claude = claude;
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
        { agent: 'opus', step: 'spec', text: 'Reading the problem' },
        () => this.claude.readSpec(project.paragraph, { sink: this.sink(projectId) }),
        (s) => `${s.design.replaceAll('_', ' ')}; ${s.outcome_type.replaceAll('_', ' ')} outcome: ${s.outcomes.join(', ')}`,
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
      await this.store.updateProject(projectId, { error: null, search_plan: null, collected: false });
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
          { agent: 'opus', step: 'search', text: 'Planning the search' },
          () => this.claude.planSearch(spec, new Toolbox(this.sources, this.activity), {
            sink: this.sink(projectId), maxIterations: this.settings.searchMaxIterations,
          }),
          (p) => `Final plan: ${plural(p.pubmed_queries.length, 'PubMed query', 'PubMed queries')}, `
            + `${plural(p.clinicaltrials_queries.length, 'ClinicalTrials.gov query', 'ClinicalTrials.gov queries')}`,
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
      await this.store.updateProject(projectId, { status: 'extracting' });
      await this.extractAll(projectId, spec);
    } catch (error) {
      await this.store.updateProject(projectId, { status: 'error', error: describeError(error) });
      await this.activity.note({ agent: 'code', step: 'run', phase: 'error', text: 'The search stopped', detail: describeError(error) });
      return;
    }
    await this.store.updateProject(projectId, { status: 'done', progress: { stage: 'done', done: 0, total: 0 } });
    const counts = await this.store.statusCounts(projectId);
    await this.activity.note({ agent: 'code', step: 'run', text: 'Search finished', detail: `${counts.extracted || 0} extracted; ready for review` });
  }

  // Run the planned queries, de-duplicate, and store the candidates.
  async collect(projectId, plan) {
    const limit = this.settings.perQueryLimit;
    const pmids = new Map();
    const trials = new Map();
    const queries = [...plan.pubmed_queries.map((q) => ['pubmed', q]), ...plan.clinicaltrials_queries.map((q) => ['clinicaltrials', q])];
    const failed = [];
    for (const [i, [source, query]] of queries.entries()) {
      await this.progress(projectId, 'running the search', i, queries.length);
      const label = `${source}: ${query}`;
      const name = source === 'pubmed' ? 'PubMed' : 'ClinicalTrials.gov';
      try {
        await this.activity.track({ agent: 'code', step: 'collect', text: `${name}: ${short(query, 90)}` }, async () => {
          if (source === 'pubmed') {
            const [, ids] = await this.sources.pubmed.search(query, limit);
            for (const id of ids) pmids.set(id, [...(pmids.get(id) || []), label]);
            return ids.length;
          }
          const [, studies] = await this.sources.trials.search(query, limit, true);
          for (const study of studies) {
            const nct = trialRecord(study).ext_id;
            const entry = trials.get(nct) || { study, foundBy: [] };
            entry.foundBy.push(label);
            trials.set(nct, entry);
          }
          return studies.length;
        }, (n) => `${plural(n, 'record')} taken`);
      } catch (error) {
        failed.push(`${label} (${describeError(error)})`);
      }
    }
    if (failed.length && failed.length === queries.length) throw new Error(`Every search query failed: ${failed.join('; ')}`);
    if (failed.length) await this.store.updateProject(projectId, { search_plan: { ...plan, failed_queries: failed } });

    for (const extra of plan.extra_ids.map((x) => x.trim())) {
      if (NCT.test(extra)) {
        const study = await this.sources.trials.study(extra.toUpperCase()).catch(() => null);
        if (study) {
          const entry = trials.get(extra.toUpperCase()) || { study, foundBy: [] };
          entry.foundBy.push('named by the search agent');
          trials.set(extra.toUpperCase(), entry);
        }
      } else if (/^\d+$/.test(extra)) {
        pmids.set(extra, [...(pmids.get(extra) || []), 'named by the search agent']);
      }
    }

    const pmidList = [...pmids.keys()].slice(0, this.settings.maxCandidates);
    const records = pmidList.length ? await this.sources.pubmed.fetch(pmidList) : [];
    const byPmid = new Map(records.map((r) => [r.pmid, r]));
    const articles = pmidList.filter((p) => byPmid.has(p)).map((p) => [byPmid.get(p), pmids.get(p)]);
    const registry = [...trials.values()].map(({ study, foundBy }) => [trialRecord(study), foundBy]);
    // Interleave the two sources so the cap on candidates keeps the top of each.
    const merged = [];
    for (let i = 0; i < Math.max(articles.length, registry.length); i++) {
      if (articles[i]) merged.push(articles[i]);
      if (registry[i]) merged.push(registry[i]);
    }
    let added = 0;
    const bySource = { pubmed: 0, clinicaltrials: 0 };
    for (const [record, foundBy] of merged) {
      if (added >= this.settings.maxCandidates) break;
      if (await this.store.addPaper(projectId, { ...record, found_by: foundBy, status: 'candidate' })) {
        added += 1;
        bySource[record.source] += 1;
      }
    }
    await this.activity.note({
      agent: 'code', step: 'collect', text: `Collected ${plural(added, 'record')} to screen`,
      detail: `${plural(bySource.pubmed, 'paper')} from PubMed, ${plural(bySource.clinicaltrials, 'registry record')} from ClinicalTrials.gov`,
    });
    await this.progress(projectId, 'running the search', queries.length, queries.length);
  }

  async screenAll(projectId, spec) {
    const papers = await this.store.papers(projectId, 'candidate');
    const sink = this.sink(projectId);
    let done = 0;
    await this.progress(projectId, 'screening', 0, papers.length);
    await pool(papers, this.settings.concurrency, async (paper) => {
      try {
        const decision = await this.activity.track(
          { agent: 'haiku', step: 'screen', text: `Screening “${short(paper.title)}”`, paperId: paper.id },
          () => this.claude.screen(spec, recordText(paper), { sink }),
          (d) => `${d.decision}: ${d.reason}`,
        );
        const status = { include: 'screened_in', exclude: 'screened_out', unsure: 'unsure' }[decision.decision];
        await this.store.updatePaper(paper.id, { status, screen: decision });
      } catch (error) {
        // Never drop a record silently: an unscreened record goes forward as "unsure".
        await this.store.updatePaper(paper.id, {
          status: 'unsure', screen: { decision: 'unsure', reason: `Screening failed (${describeError(error)}); review by hand.` },
        });
      }
    }, () => this.progress(projectId, 'screening', ++done, papers.length));
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
    await pool(queue, this.settings.concurrency, (paper) => this.extractPaper(projectId, spec, paper.id),
      () => this.progress(projectId, 'extracting', ++done, queue.length));
  }

  // -- one paper ------------------------------------------------------------------

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
    if (paper.doi) {
      const url = await sources.unpaywall.pdfUrl(paper.doi);
      if (url) {
        const bytes = await sources.unpaywall.downloadPdf(url);
        if (bytes) {
          return {
            kind: 'oa_pdf', text: await pdfText(bytes), url, pdf: bytesToBase64(bytes),
            fetched_by: sources.http.isBlocked(url) ? 'sonnet' : 'browser',
          };
        }
      }
    }
    if (paper.abstract) {
      return { kind: 'abstract', text: `# ${paper.title}\n\n## Abstract\n${paper.abstract}\n`, url: `https://pubmed.ncbi.nlm.nih.gov/${paper.ext_id}/` };
    }
    return null;
  }

  // upload: the bytes of a PDF the user supplied, used instead of searching for the text.
  async extractPaper(projectId, spec, paperId, { upload = null, uploadName = '' } = {}) {
    try {
      await this.extractOne(projectId, spec, paperId, upload, uploadName);
    } catch (error) {
      await this.store.updatePaper(paperId, { status: 'error', error: `Extraction failed. ${describeError(error)}` });
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
          (d) => (d ? `${TEXT_KIND[d.kind]}${d.fetched_by === 'sonnet' ? ', fetched by Sonnet' : ''}` : 'no open text found'),
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
      status: 'extracting', fulltext_kind: doc.kind, fulltext_url: doc.url, fetched_by: doc.fetched_by || null,
    });

    const document = documentBlock(doc, paper.title || paper.ext_id);
    const quotesReliable = !doc.pdf && Boolean(doc.text.trim());
    let worker = null;
    let report = null;
    let workerError = null;
    try {
      worker = await this.activity.track(
        { agent: 'haiku', step: 'extract', text: `Extracting “${title}”`, paperId },
        () => this.claude.extract(spec, document, { sink }),
        (x) => `${plural(x.results.length, 'result')}, ${plural(countValues(x), 'value')}`,
      );
      report = verify(worker, doc.text, quotesReliable);
    } catch (error) {
      if (error instanceof ModelDeclined) workerError = `the worker ${error.message}`;
      else if (error instanceof ModelTruncated) workerError = "the worker's extraction was cut off at max_tokens";
      else workerError = `the worker failed: ${describeError(error)}`;
    }
    let reasons = workerError ? [workerError] : escalationReasons(worker, report);
    const audited = !reasons.length && await inAuditSample(`${projectId}:${paperId}`, this.settings.auditFraction);
    if (audited) reasons = ['random audit of an extraction that passed every check'];
    if (worker) {
      await this.activity.note({
        agent: 'code', step: 'verify', paperId, phase: reasons.length ? 'flag' : 'note',
        text: `Checked “${title}”`,
        detail: audited ? 'every check passed; picked for the random audit, so Opus reviews it'
          : reasons.length ? `${plural(reasons.length, 'flag')}, so Opus reviews it: ${short(reasons[0], 120)}`
            : 'every quote found and every number plausible',
      });
    }

    let final = worker;
    let finalReport = report;
    let extractedBy = 'worker';
    if (reasons.length) {
      try {
        final = await this.activity.track(
          { agent: 'opus', step: 'adjudicate', text: `Reviewing “${title}”`, paperId },
          () => this.claude.adjudicate(spec, document, worker, reasons, { sink }),
          (x) => `${plural(x.results.length, 'result')}, ${plural(countValues(x), 'value')}`,
        );
        finalReport = verify(final, doc.text, quotesReliable);
        extractedBy = 'manager';
      } catch (error) {
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
      status: 'extracted', extraction: final, extracted_by: extractedBy, verification: finalReport,
      escalation: reasons.length ? { reasons, audit: audited } : null,
    });
  }
}

// What the search agent can call while it explores. Each call shows in the activity log as Opus's.
export class Toolbox {
  constructor(sources, activity = NO_ACTIVITY) {
    this.sources = sources;
    this.activity = activity;
  }

  previewPubmed(query, maxResults) {
    return this.activity.track({ agent: 'opus', step: 'search', text: `Tries PubMed: ${short(query, 90)}` }, async () => {
      const [count, ids] = await this.sources.pubmed.search(query, maxResults);
      const lines = [`Total hits: ${count}`];
      for (const r of await this.sources.pubmed.summaries(ids)) lines.push(`${r.pmid} | ${r.year} | ${r.pubtypes.join(', ')} | ${r.title}`);
      return lines.join('\n');
    }, (out) => out.split('\n')[0].replace('Total hits:', 'hits:'));
  }

  previewTrials(query, maxResults) {
    return this.activity.track({ agent: 'opus', step: 'search', text: `Tries ClinicalTrials.gov: ${short(query, 90)}` },
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
