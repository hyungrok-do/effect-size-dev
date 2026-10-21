// Turn JATS full-text XML (PubMed Central, Europe PMC) into plain text that keeps tables readable:
// each table row becomes one line with cells separated by " | ", so a quote of a whole row can be
// found verbatim. A port of python/esf/jats.py.

const FLOATS = new Set(['table-wrap', 'fig', 'table-wrap-group', 'fig-group']);
const SKIP = new Set(['ref-list', 'fn-group', 'ack', 'glossary', 'notes', 'xref-list']);

const clean = (text) => text.split(/\s+/).filter(Boolean).join(' ');
const kids = (el) => [...el.children];
const child = (el, name) => kids(el).find((c) => c.localName === name) || null;
const path = (el, ...names) => names.reduce((at, name) => (at ? child(at, name) : null), el);
const descendants = (el, name) => [...el.getElementsByTagName('*')].filter((c) => c.localName === name);
const allText = (el) => (el ? clean(el.textContent) : '');

// Text of an element, leaving out floats (tables, figures) nested inside it.
function inlineText(el) {
  let out = '';
  for (const node of el.childNodes) {
    if (node.nodeType === 3) out += node.data;
    else if (node.nodeType === 1 && !FLOATS.has(node.localName)) out += inlineText(node);
  }
  return out;
}

function table(wrap, out) {
  const label = allText(child(wrap, 'label'));
  const caption = child(wrap, 'caption');
  out.push(`[${label || 'Table'}] ${caption ? clean(inlineText(caption)) : ''}`.trimEnd());
  for (const tab of descendants(wrap, 'table')) {
    for (const tr of descendants(tab, 'tr')) {
      const cells = kids(tr).filter((c) => c.localName === 'td' || c.localName === 'th').map((c) => allText(c));
      if (cells.some(Boolean)) out.push(cells.join(' | '));
    }
  }
  const foot = child(wrap, 'table-wrap-foot');
  if (foot && allText(foot)) out.push(`Table note: ${allText(foot)}`);
  out.push('');
}

function figure(fig, out) {
  const label = allText(child(fig, 'label')) || 'Figure';
  out.push(`[${label}] ${allText(child(fig, 'caption'))} (figure image not included)`);
  out.push('');
}

function walkFloat(el, out) {
  if (el.localName === 'table-wrap') table(el, out);
  else if (el.localName === 'fig') figure(el, out);
  else for (const c of kids(el)) walkFloat(c, out);
}

function walk(el, out, depth) {
  for (const c of kids(el)) {
    const tag = c.localName;
    if (SKIP.has(tag) || tag === 'title') continue;
    if (tag === 'sec') {
      const title = child(c, 'title');
      if (title) out.push(`${'#'.repeat(Math.min(depth + 2, 6))} ${allText(title)}`);
      walk(c, out, depth + 1);
    } else if (tag === 'p') {
      const text = clean(inlineText(c));
      if (text) out.push(text, '');
      for (const f of kids(c)) if (FLOATS.has(f.localName)) walkFloat(f, out);
    } else if (FLOATS.has(tag)) {
      walkFloat(c, out);
    } else if (tag === 'list') {
      for (const item of descendants(c, 'list-item')) {
        const text = clean(inlineText(item));
        if (text) out.push(`- ${text}`);
      }
      out.push('');
    } else {
      walk(c, out, depth);
    }
  }
}

export function parseXml(xml) {
  const doc = new DOMParser().parseFromString(xml, 'application/xml');
  if (doc.getElementsByTagName('parsererror').length) throw new Error('The XML could not be read.');
  return doc;
}

export function jatsToText(xml) {
  const root = parseXml(xml).documentElement;
  const out = [];
  const meta = path(root, 'front', 'article-meta');
  const title = meta && path(meta, 'title-group', 'article-title');
  if (title) out.push(`# ${allText(title)}`, '');
  for (const abstract of meta ? kids(meta).filter((c) => c.localName === 'abstract') : []) {
    if (['graphical', 'teaser'].includes(abstract.getAttribute('abstract-type'))) continue;
    out.push('## Abstract');
    walk(abstract, out, 1);
    out.push('');
  }
  const body = child(root, 'body');
  if (body) walk(body, out, 0);
  const floats = child(root, 'floats-group');
  if (floats) for (const c of kids(floats)) walkFloat(c, out);
  const back = child(root, 'back');
  if (back) for (const app of descendants(back, 'app')) walk(app, out, 1);
  return `${out.join('\n').trim()}\n`;
}
