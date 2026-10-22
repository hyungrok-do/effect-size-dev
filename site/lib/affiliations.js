// Author affiliations as institutions. PubMed and Europe PMC affiliation strings carry more than
// the institution: an email address (often after "Electronic address:"), a footnote marker before it
// ("1Department", "a Department"), and in older records several institutions run together with ";".
// Used when records are read (sources.js) and when an author table is shown (review-view.js), so
// author lists saved before the cleaning look right too. Mirrors clean_affiliations in
// python/esf/sources.py; the tests require identical output.

const EMAIL_LABEL = /\s*\b(?:electronic address|e-?mail(?: address)?|email)\s*:?\s*(?=\S+@)/gi;
const EMAIL = /\s*\S+@\S+/g;
const MARKER = /^(?:\d{1,2}|[a-h])\s?(?=[A-Z][a-z])/;

// Each institution once, in order, without email addresses or footnote markers.
export function cleanAffiliations(texts) {
  const out = [];
  for (const text of texts) {
    for (const raw of String(text ?? '').split(/;\s*/)) {
      let part = raw.replace(EMAIL_LABEL, ' ').replace(EMAIL, '');
      part = part.split(/\s+/).filter(Boolean).join(' ').replace(MARKER, '');
      part = part.replace(/[\s,;:(]+$/, '').trim();
      if (part && part !== '.' && !out.includes(part)) out.push(part);
    }
  }
  return out;
}
