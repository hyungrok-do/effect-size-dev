// DOM and export validation without a browser process; run by test_reporting.py.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { createRequire } from 'node:module';
const require = createRequire(new URL('../site/package.json', import.meta.url));
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!doctype html><body><main id="report"></main></body>', { url: 'http://localhost/' });
Object.assign(globalThis, { document: dom.window.document, Node: dom.window.Node });
const downloads = [], blobs = new Map();
URL.createObjectURL = (blob) => { const id = `blob:test-${blobs.size}`; blobs.set(id, blob); return id; };
URL.revokeObjectURL = () => {};
dom.window.HTMLAnchorElement.prototype.click = function () { downloads.push({ name: this.download, blob: blobs.get(this.href) }); };
const { evidenceTablesView } = await import('../site/lib/report-view.js');
const { finalizeTables } = await import('../site/lib/reporting.js');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const tables = finalizeTables(input.plan, input.evidence);
const root = document.querySelector('#report');
root.append(...evidenceTablesView(tables, input.evidence, { stale: true }));
assert.equal(root.querySelectorAll('table').length, 6);
assert.equal(root.querySelectorAll('[data-table="population-findings"] tbody tr').length, tables.findings.length);
assert.equal(root.querySelectorAll('[data-table="analysis-extraction"] tbody tr').length, 2);
assert.equal(root.querySelectorAll('[data-table="reported-values"] tbody tr').length, 6);
assert(root.querySelector('.stale'));
assert.equal(root.querySelectorAll('.planning-recommendation,.summary-overview,img,script').length, 0);
for (const a of root.querySelectorAll('a[href^="#evidence-value-"]')) assert(document.getElementById(a.getAttribute('href').slice(1)));
root.querySelector('[data-table="population-findings"] button').click();
assert.equal(downloads[0].name, 'population-findings.csv');
const csv = await downloads[0].blob.text();
assert(csv.startsWith('Population,Comparison,Outcome / time,Target parameter,'));
assert(csv.includes('Not calculated'));
assert(csv.includes('risk_difference = -0.1'));
assert(csv.includes('ci_lower = -0.18 (95% CI)'));
assert(csv.includes("'=HYPERLINK")); // text cells cannot execute spreadsheet formulas
assert(csv.includes('\nAnalysis set: ')); // multiline cells remain legible
console.log(JSON.stringify({ tables: 6, findings: tables.findings.length, values: 6, csv: downloads[0].name }));
