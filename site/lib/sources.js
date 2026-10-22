// Literature sources: PubMed, ClinicalTrials.gov, Europe PMC and Unpaywall, called from the browser.
// A site that doesn't allow requests from other web pages (CORS) is fetched through Claude's web
// fetch tool instead (see Claude.fetchUrl), which runs on Anthropic's servers. A port of
// python/esf/sources.py.

import { parseXml } from './jats.js';

export class SourceError extends Error {}

const MAX_PDF_BYTES = 20 * 1024 * 1024; // base64 adds a third; a Claude API request may be 32 MB
const PROXY_URL_LIMIT = 250; // the longest URL the web fetch tool accepts
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const clean = (text) => String(text ?? '').split(/\s+/).filter(Boolean).join(' ');
// Python's str.capitalize: the first letter upper case, the rest lower.
const capitalize = (s) => (s ? s[0].toUpperCase() + s.slice(1).toLowerCase() : '');

export function base64ToBytes(b64) {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes;
}

export function bytesToBase64(bytes) {
  let bin = '';
  for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(bin);
}

const isPdf = (bytes) => bytes.length > 4 && bytes[0] === 0x25 && bytes[1] === 0x50 && bytes[2] === 0x44 && bytes[3] === 0x46;

export class Http {
  // proxy(url) -> {kind: 'text', text} | {kind: 'pdf', base64}; it throws SourceError when it can't.
  constructor({ proxy = null, fetchImpl = (...a) => fetch(...a) } = {}) {
    this.proxy = proxy;
    this.fetch = fetchImpl;
    this.blocked = new Set(); // hosts that refused a direct request
    this.proxied = 0;
  }

  isBlocked(url) {
    return this.blocked.has(new URL(url).host);
  }

  async viaProxy(url, as) {
    if (!this.proxy) throw new SourceError(`${new URL(url).host} doesn't accept requests from web pages.`);
    if (url.length > PROXY_URL_LIMIT) throw new SourceError(`${new URL(url).host} doesn't accept requests from web pages, and the address is too long to fetch through Claude.`);
    this.proxied += 1;
    const doc = await this.proxy(url);
    if (as === 'pdf') {
      if (doc.kind !== 'pdf') throw new SourceError('Claude fetched the address, but it was not a PDF.');
      return base64ToBytes(doc.base64);
    }
    if (doc.kind !== 'text') throw new SourceError('Claude fetched the address, but it returned a PDF.');
    if (as === 'json') {
      try {
        return JSON.parse(doc.text);
      } catch {
        throw new SourceError('Claude fetched the address, but the reply was not JSON.');
      }
    }
    return doc.text;
  }

  // as: 'json' | 'text' | 'pdf'; returns null for a 404.
  async get(url, as = 'json') {
    if (this.isBlocked(url)) return this.viaProxy(url, as);
    let delay = 1000;
    for (let attempt = 0; ; attempt++) {
      let response;
      try {
        response = await this.fetch(url);
      } catch {
        // A TypeError here is how browsers report a refused cross-site request (or no network).
        this.blocked.add(new URL(url).host);
        return this.viaProxy(url, as);
      }
      if ([429, 500, 502, 503, 504].includes(response.status) && attempt < 3) {
        await sleep(delay);
        delay *= 2;
        continue;
      }
      if (response.status === 404) return null;
      if (!response.ok) throw new SourceError(`${new URL(url).host} returned HTTP ${response.status}`);
      if (as === 'json') return response.json();
      if (as === 'text') return response.text();
      const length = Number(response.headers.get('content-length') || 0);
      if (length > MAX_PDF_BYTES) return null;
      const bytes = new Uint8Array(await response.arrayBuffer());
      return bytes.length <= MAX_PDF_BYTES && isPdf(bytes) ? bytes : null;
    }
  }
}

const yearOf = (text) => (String(text || '').match(/[12]\d{3}/) || [''])[0];

// The first three authors, then "et al."; how the overlap check tells reports of one study apart.
export function shortAuthors(names) {
  const kept = names.filter(Boolean);
  return kept.slice(0, 3).join(', ') + (kept.length > 3 ? ', et al.' : '');
}

export function makeRecord(fields) {
  return {
    title: '', authors: '', year: '', journal: '', pubtypes: [], abstract: '', doi: null, pmid: null, pmcid: null, nct_ids: [],
    // Every author with their affiliations ("; "-joined), and for a registry record the officials with
    // their roles: {name, affiliation, role?}. Shown in the review's author table.
    author_list: [],
    ...fields,
  };
}

// -- PubMed -----------------------------------------------------------------------

export class PubMed {
  static BASE = 'https://eutils.ncbi.nlm.nih.gov/entrez/eutils/';

  constructor(http, { email, apiKey } = {}) {
    this.http = http;
    this.common = { tool: 'effect-size-finder' };
    if (email) this.common.email = email;
    if (apiKey) this.common.api_key = apiKey;
    // NCBI allows 3 requests a second without a key and 10 with one.
    this.minInterval = apiKey ? 110 : 340;
    this.queue = Promise.resolve();
  }

  url(endpoint, params) {
    return `${PubMed.BASE}${endpoint}?${new URLSearchParams({ ...params, ...this.common })}`;
  }

  call(endpoint, params, as) {
    const turn = this.queue.then(() => sleep(this.minInterval));
    this.queue = turn;
    return turn.then(() => this.http.get(this.url(endpoint, params), as));
  }

  async search(term, retmax) {
    const payload = await this.call('esearch.fcgi', { db: 'pubmed', term, retmax, retmode: 'json', sort: 'relevance' }, 'json');
    const result = payload?.esearchresult || {};
    if (result.ERROR) throw new SourceError(`PubMed rejected the query: ${result.ERROR}`);
    return [Number(result.count || 0), result.idlist || []];
  }

  chunkSize() {
    // Through Claude's web fetch an address may be at most 250 characters long.
    if (!this.http.isBlocked(PubMed.BASE)) return 200;
    const base = this.url('efetch.fcgi', { db: 'pubmed', retmode: 'xml', id: '' }).length;
    return Math.max(1, Math.floor((PROXY_URL_LIMIT - base) / 9));
  }

  async summaries(pmids) {
    const records = [];
    for (let i = 0; i < pmids.length; i += this.chunkSize()) {
      const chunk = pmids.slice(i, i + this.chunkSize());
      const payload = await this.call('esummary.fcgi', { db: 'pubmed', id: chunk.join(','), retmode: 'json' }, 'json');
      const result = payload?.result || {};
      for (const uid of result.uids || []) {
        const item = result[uid] || {};
        const ids = Object.fromEntries((item.articleids || []).map((a) => [a.idtype, a.value]));
        records.push(makeRecord({
          source: 'pubmed', ext_id: uid, pmid: uid, title: item.title || '', year: yearOf(item.pubdate || item.sortpubdate),
          journal: item.source || '', pubtypes: item.pubtype || [], doi: ids.doi || null, pmcid: ids.pmc || ids.pmcid || null,
        }));
      }
    }
    return records;
  }

  // Full records, abstracts included.
  async fetch(pmids) {
    const records = [];
    for (let i = 0; i < pmids.length; i += this.chunkSize()) {
      const chunk = pmids.slice(i, i + this.chunkSize());
      const xml = await this.call('efetch.fcgi', { db: 'pubmed', id: chunk.join(','), retmode: 'xml' }, 'text');
      if (xml) records.push(...parsePubmedXml(xml));
    }
    return records;
  }

  // PMIDs an article cites, from its reference list in PubMed Central (none when PMC has no list).
  async references(pmid) {
    const payload = await this.call('elink.fcgi', {
      dbfrom: 'pubmed', db: 'pubmed', id: pmid, linkname: 'pubmed_pubmed_refs', retmode: 'json',
    }, 'json');
    for (const linkset of payload?.linksets || []) {
      for (const links of linkset.linksetdbs || []) {
        if (links.linkname === 'pubmed_pubmed_refs') return (links.links || []).map(String);
      }
    }
    return [];
  }
}

const kids = (el, name) => [...(el?.children || [])].filter((c) => c.localName === name);
const kid = (el, name) => kids(el, name)[0] || null;
const at = (el, ...names) => names.reduce((node, name) => kid(node, name), el);
const text = (el) => clean(el?.textContent);

export function parsePubmedXml(xml) {
  const doc = parseXml(xml);
  const records = [];
  for (const article of doc.getElementsByTagName('PubmedArticle')) {
    const citation = kid(article, 'MedlineCitation');
    const pmid = text(kid(citation, 'PMID'));
    const art = kid(citation, 'Article');
    if (!art || !pmid) continue;
    const parts = kids(kid(art, 'Abstract'), 'AbstractText').map((block) => {
      const label = block.getAttribute('Label');
      return label ? `${label}: ${text(block)}` : text(block);
    }).filter((p) => p && !p.endsWith(': '));
    const year = text(at(art, 'Journal', 'JournalIssue', 'PubDate', 'Year'))
      || yearOf(text(at(art, 'Journal', 'JournalIssue', 'PubDate', 'MedlineDate')))
      || text(at(art, 'ArticleDate', 'Year'));
    const ids = Object.fromEntries(kids(at(article, 'PubmedData', 'ArticleIdList'), 'ArticleId').map((a) => [a.getAttribute('IdType'), text(a)]));
    let doi = ids.doi;
    if (!doi) doi = kids(art, 'ELocationID').filter((e) => e.getAttribute('EIdType') === 'doi').map(text)[0];
    // A name published in error (ValidYN="N") is left out; the corrected one is listed too.
    const authorEls = kids(kid(art, 'AuthorList'), 'Author').filter((author) => author.getAttribute('ValidYN') !== 'N');
    const names = authorEls.map((author) => {
      const last = text(kid(author, 'LastName'));
      return last ? `${last} ${text(kid(author, 'Initials'))}`.trim() : text(kid(author, 'CollectiveName'));
    });
    const authorList = authorEls.map((author) => {
      const last = text(kid(author, 'LastName'));
      const first = text(kid(author, 'ForeName')) || text(kid(author, 'Initials'));
      const name = last ? `${first} ${last}`.trim() : text(kid(author, 'CollectiveName'));
      const affiliations = kids(author, 'AffiliationInfo').flatMap((info) => kids(info, 'Affiliation')).map(text).filter(Boolean);
      return { name, affiliation: affiliations.join('; ') };
    }).filter((a) => a.name);
    const nctIds = kids(kid(art, 'DataBankList'), 'DataBank')
      .filter((bank) => text(kid(bank, 'DataBankName')) === 'ClinicalTrials.gov')
      .flatMap((bank) => kids(kid(bank, 'AccessionNumberList'), 'AccessionNumber').map(text))
      .filter(Boolean);
    records.push(makeRecord({
      source: 'pubmed', ext_id: pmid, pmid, title: text(kid(art, 'ArticleTitle')), authors: shortAuthors(names), year,
      journal: text(at(art, 'Journal', 'Title')) || text(at(art, 'Journal', 'ISOAbbreviation')),
      pubtypes: kids(kid(art, 'PublicationTypeList'), 'PublicationType').map(text),
      abstract: parts.join('\n'), doi: doi || null, pmcid: ids.pmc || null, nct_ids: nctIds, author_list: authorList,
    }));
  }
  return records;
}

// -- ClinicalTrials.gov -----------------------------------------------------------

const DISPERSION = {
  STANDARD_DEVIATION: 'SD', STANDARD_ERROR: 'SE', INTER_QUARTILE_RANGE: 'IQR', FULL_RANGE: 'range',
  GEOMETRIC_COEFFICIENT_OF_VARIATION: 'geometric CV',
};

export class ClinicalTrials {
  static BASE = 'https://clinicaltrials.gov/api/v2/studies';

  constructor(http) {
    this.http = http;
  }

  async search(term, limit, withResults = true) {
    const params = { 'query.term': term, pageSize: String(Math.min(limit, 100)), countTotal: 'true', format: 'json' };
    if (withResults) params.aggFilters = 'results:with';
    const studies = [];
    let total = 0;
    while (studies.length < limit) {
      const payload = await this.http.get(`${ClinicalTrials.BASE}?${new URLSearchParams(params)}`, 'json');
      if (!payload) break;
      total = payload.totalCount ?? total;
      studies.push(...(payload.studies || []));
      if (!payload.nextPageToken) break;
      params.pageToken = payload.nextPageToken;
    }
    return [total, studies.slice(0, limit)];
  }

  study(nctId) {
    return this.http.get(`${ClinicalTrials.BASE}/${encodeURIComponent(nctId)}?format=json`, 'json');
  }
}

export function trialRecord(study) {
  const protocol = study.protocolSection || {};
  const ident = protocol.identificationModule || {};
  const status = protocol.statusModule || {};
  const design = protocol.designModule || {};
  const nct = ident.nctId || '';
  const outcomes = protocol.outcomesModule || {};
  const lines = [
    `Status: ${status.overallStatus || 'unknown'}; results posted: ${study.hasResults ? 'yes' : 'no'}`,
    `Study type: ${design.studyType || ''}; allocation: ${design.designInfo?.allocation || ''}; enrollment: ${design.enrollmentInfo?.count ?? ''}`,
    `Conditions: ${(protocol.conditionsModule?.conditions || []).join(', ')}`,
    `Interventions: ${(protocol.armsInterventionsModule?.interventions || []).map((i) => i.name || '').join(', ')}`,
  ];
  for (const [kind, label] of [['primaryOutcomes', 'Primary'], ['secondaryOutcomes', 'Secondary']]) {
    for (const o of (outcomes[kind] || []).slice(0, 10)) lines.push(`${label} outcome: ${o.measure || ''} (${o.timeFrame || ''})`);
  }
  const summary = protocol.descriptionModule?.briefSummary;
  if (summary) lines.push(`Summary: ${clean(summary)}`);
  const pmids = (protocol.referencesModule?.references || []).filter((r) => r.pmid && r.type === 'RESULT').map((r) => r.pmid);
  if (pmids.length) lines.push(`Result publications (PMID): ${pmids.join(', ')}`);
  const officials = (protocol.contactsLocationsModule?.overallOfficials || [])
    .map((o) => ({ name: clean(o.name), affiliation: clean(o.affiliation), role: capitalize(String(o.role || '').replaceAll('_', ' ')) }))
    .filter((o) => o.name);
  return makeRecord({
    source: 'clinicaltrials', ext_id: nct, title: ident.officialTitle || ident.briefTitle || '',
    authors: shortAuthors(officials.map((o) => o.name)), year: yearOf(status.startDateStruct?.date), journal: 'ClinicalTrials.gov',
    pubtypes: design.studyType ? [design.studyType] : [], abstract: lines.join('\n'), nct_ids: [nct], author_list: officials,
  });
}

// The posted results of a registry record, written out so each number sits with its labels.
export function trialResultsText(study) {
  const record = trialRecord(study);
  const out = [`# ${record.ext_id}: ${record.title}`, '', record.abstract, ''];
  const measures = study.resultsSection?.outcomeMeasuresModule?.outcomeMeasures || [];
  if (!measures.length) out.push('No outcome results are posted for this study.');
  for (const om of measures) {
    const groups = Object.fromEntries((om.groups || []).map((g) => [g.id, g.title || g.id]));
    const name = (id) => groups[id] ?? id;
    out.push(`## Outcome measure (${(om.type || '').toLowerCase()}): ${om.title || ''}`);
    if (om.description) out.push(`Description: ${clean(om.description)}`);
    out.push(`Time frame: ${om.timeFrame || ''}`);
    const dispersion = om.dispersionType || '';
    out.push(`Parameter: ${om.paramType || ''}; dispersion: ${DISPERSION[dispersion] ?? dispersion}; unit: ${om.unitOfMeasure || ''}`);
    for (const denom of om.denoms || []) {
      out.push(`Analyzed (${(denom.units || 'participants').toLowerCase()}): ${(denom.counts || []).map((c) => `${name(c.groupId)}: ${c.value}`).join(' | ')}`);
    }
    for (const cls of om.classes || []) {
      for (const category of cls.categories || []) {
        const label = [cls.title, category.title].filter(Boolean).join(' / ');
        const cells = (category.measurements || []).map((m) => {
          let cell = `${name(m.groupId)}: ${m.value ?? ''}`;
          if (m.spread) cell += ` (${DISPERSION[dispersion] ?? 'spread'} ${m.spread})`;
          else if (m.lowerLimit || m.upperLimit) cell += ` (${DISPERSION[dispersion] ?? dispersion.replaceAll('_', ' ').toLowerCase()} ${m.lowerLimit ?? ''} to ${m.upperLimit ?? ''})`;
          return cell;
        });
        out.push(`${label ? `${label}: ` : 'Values: '}${cells.join(' | ')}`);
      }
    }
    for (const a of om.analyses || []) {
      const parts = [`Analysis (${(a.groupIds || []).map(name).join(' vs ')})`];
      if (a.paramType) parts.push(`${a.paramType} ${a.paramValue ?? ''}`);
      if (a.ciLowerLimit || a.ciUpperLimit) parts.push(`${a.ciPctValue ?? ''}% CI ${a.ciLowerLimit ?? ''} to ${a.ciUpperLimit ?? ''}`);
      if (a.pValue) parts.push(`p = ${a.pValue}`);
      if (a.statisticalMethod) parts.push(`method: ${a.statisticalMethod}`);
      out.push(parts.join('; '));
    }
    out.push('');
  }
  return `${out.join('\n').trim()}\n`;
}

// -- Europe PMC and Unpaywall -------------------------------------------------------

// Europe PMC kinds kept from a search: PubMed records, articles only in PubMed Central, preprints.
const EUROPEPMC_KINDS = new Set(['MED', 'PMC', 'PPR']);
// Europe PMC titles and abstracts carry HTML tags (<i>, <h4>Background</h4>).
const plain = (html) => clean(String(html || '').replace(/<[^>]+>/g, ' '));

export const europepmcPmid = (result) => String(result.pmid || (result.source === 'MED' ? result.id : '')) || null;

const europepmcNames = (result) => String(result.authorString || '').split(',').map((n) => clean(n).replace(/\.+$/, '')).filter(Boolean);

// A Europe PMC result's authors with their affiliations; from the names alone when the result has no
// author list (a "lite" result).
export function europepmcAuthors(result) {
  const out = [];
  for (const a of result.authorList?.author || []) {
    const first = clean(a.firstName) || clean(a.initials); // as PubMed's: the initials when there is no first name
    const last = clean(a.lastName);
    const name = last ? clean(`${first} ${last}`) : clean(a.fullName) || clean(a.collectiveName);
    let affiliations = (a.authorAffiliationDetailsList?.authorAffiliation || []).map((d) => clean(d.affiliation)).filter(Boolean);
    if (!affiliations.length && clean(a.affiliation)) affiliations = [clean(a.affiliation)];
    if (name) out.push({ name, affiliation: affiliations.join('; ') });
  }
  return out.length ? out : europepmcNames(result).map((name) => ({ name, affiliation: '' }));
}

// A Europe PMC result that is not in PubMed, as a record.
export function europepmcRecord(result) {
  const types = result.pubTypeList?.pubType || [];
  return makeRecord({
    source: 'europepmc', ext_id: String(result.id), title: plain(result.title),
    authors: shortAuthors(europepmcNames(result)),
    year: yearOf(String(result.pubYear ?? '')),
    journal: result.journalInfo?.journal?.title || result.bookOrReportDetails?.publisher || '',
    pubtypes: typeof types === 'string' ? [types] : [...types], abstract: plain(result.abstractText),
    doi: result.doi ?? null, pmcid: result.pmcid ?? null, author_list: europepmcAuthors(result),
  });
}

export function europepmcUrl(extId) {
  const kind = extId.startsWith('PPR') ? 'PPR' : extId.startsWith('PMC') ? 'PMC' : 'MED';
  return `https://europepmc.org/article/${kind}/${extId}`;
}

export class EuropePMC {
  static BASE = 'https://www.ebi.ac.uk/europepmc/webservices/rest/';

  constructor(http) {
    this.http = http;
  }

  // Results of a Europe PMC query, by relevance: PubMed records (MED), articles only in PubMed Central
  // (PMC) and preprints (PPR). Europe PMC searches the full text of open-access articles too.
  async search(query, limit, { abstracts = true } = {}) {
    const params = new URLSearchParams({
      query, format: 'json', pageSize: String(Math.max(1, Math.min(limit, 1000))), resultType: abstracts ? 'core' : 'lite',
    });
    const payload = await this.http.get(`${EuropePMC.BASE}search?${params}`, 'json');
    const results = (payload?.resultList?.result || []).filter((r) => EUROPEPMC_KINDS.has(r.source) && r.id);
    return [Number(payload?.hitCount || 0), results.slice(0, limit)];
  }

  async fulltextXml(pmcid) {
    const id = /^pmc/i.test(pmcid) ? pmcid : `PMC${pmcid}`;
    const xml = await this.http.get(`${EuropePMC.BASE}${id}/fullTextXML`, 'text');
    return xml && xml.trimStart().startsWith('<') ? xml : null;
  }

  // PMIDs an article cites, as far as Europe PMC matched its reference list to PubMed.
  async references(pmid) {
    const params = new URLSearchParams({ page: '1', pageSize: '1000', format: 'json' });
    const payload = await this.http.get(`${EuropePMC.BASE}MED/${pmid}/references?${params}`, 'json');
    return (payload?.referenceList?.reference || []).filter((r) => r.source === 'MED' && r.id).map((r) => String(r.id));
  }
}

export class Unpaywall {
  static BASE = 'https://api.unpaywall.org/v2/';

  constructor(http, email) {
    this.http = http;
    this.email = email;
  }

  async pdfUrl(doi) {
    if (!this.email) return null;
    const payload = await this.http.get(`${Unpaywall.BASE}${encodeURI(doi)}?${new URLSearchParams({ email: this.email })}`, 'json');
    if (!payload) return null;
    for (const loc of [payload.best_oa_location, ...(payload.oa_locations || [])]) {
      if (loc?.url_for_pdf) return loc.url_for_pdf;
    }
    return null;
  }

  downloadPdf(url) {
    return this.http.get(url, 'pdf');
  }
}

export class Sources {
  constructor({ email = '', ncbiApiKey = '', proxy = null, fetchImpl } = {}) {
    this.http = new Http({ proxy, fetchImpl });
    this.pubmed = new PubMed(this.http, { email, apiKey: ncbiApiKey });
    this.trials = new ClinicalTrials(this.http);
    this.europepmc = new EuropePMC(this.http);
    this.unpaywall = new Unpaywall(this.http, email);
  }

  // PMIDs cited by an article: Europe PMC's reference list, else PubMed Central's.
  async references(pmid) {
    const found = await this.europepmc.references(pmid).catch(() => []);
    return found.length ? found : this.pubmed.references(pmid);
  }
}
