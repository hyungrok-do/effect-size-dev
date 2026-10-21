# Effect Size Finder

Describe a study you are planning in a short paragraph. Effect Size Finder searches PubMed and
ClinicalTrials.gov, reads the papers it can open, and extracts the statistics they report (group
sizes, means, SDs, event counts, CIs, hazard ratios, and so on), each with the sentence or table
row it came from. You review the results, include or exclude each one, and export what you keep.

This first version does two things: **search for relevant papers** and **extract the reported
numbers**. It does not pool them or compute a sample size yet.

This project was largely generated with Claude Code and is under validation.

## Two editions

| | Browser edition (`site/`) | Local server (`python/`, `web/`) |
|---|---|---|
| Runs | Entirely in the page; published on GitHub Pages | A Python server on your computer |
| Install | None: open the page | `pip install -e .` and `esf serve` |
| API key | Each person pastes their own; it goes only to Anthropic | Pasted in the page, or set on the server |
| Searches stored | In that person's browser (IndexedDB) | In `data/` on the server |
| PDFs the browser can't download | Fetched by Claude Sonnet 5.5 (web fetch) | Downloaded by the server |

Both give Claude the same prompts, schemas and tools: `scripts/build_site_data.py` writes them
from the Python package into `site/lib/generated.js`. The browser edition's quote checks, JATS
reader and registry formatting are ports of the Python ones, and the tests require identical output.

## How it works

```
paragraph
  ▼
OPUS    Read the problem → search specification (PICO, design, outcome type, statistics needed)
YOU     Check and edit the specification
OPUS    Plan the search from the top down: concept blocks, hit counts, then one query per level
CODE    Run the levels in order (reviews → target population → wider), then ClinicalTrials.gov
HAIKU   Screen every title and abstract: include / exclude / unsure
CODE    Add the studies cited by the reviews that passed screening; Haiku screens those too
CODE    Get text: posted registry results → PMC full text (Europe PMC) → open-access PDF (Unpaywall) → abstract
SONNET  (browser edition) Download what the browser may not: PDFs, and any site that refuses web pages
HAIKU   Extract every reported number with its verbatim quote and location
CODE    Check each quote is in the source and each number is in its quote; plausibility checks
OPUS    Re-extract anything that failed a check, was flagged, or fell in the random audit sample
YOU     Include or exclude each result; export CSV or JSON
```

Claude reads and quotes; it never computes or converts a number. Every value is checked by code
against the text it was read from.

### The search, from the top down

A search that ANDs every detail of the plan together finds almost nothing: abstracts often leave
out the instrument (PHQ-9) or the comparator. For a mindfulness app versus waitlist for PHQ-9 in
university students, PubMed returned 1 record for all of it together (October 2026), but 29 reviews
of mindfulness apps for depression, 32 trials of them in students and 216 in any population. So Opus builds the
search the way a librarian would:

1. **Concept blocks.** One block each for the intervention, the condition and the population, each
   a MeSH heading ORed with free-text synonyms. Opus checks each block's hit count (the
   `count_pubmed` tool) before combining them, and never makes a block from the comparator or an
   instrument name.
2. **Levels**, run in this order:
   - **Reviews and meta-analyses** of the intervention and condition, in any population. They give
     pooled effects and list the trials they include.
   - **Studies in the target population**: population, intervention and condition, with a filter
     for the planned design (trials, or cohort and case-control studies).
   - **Studies in related populations**, the same with the narrowest concept relaxed, when the
     target population has few.
   - **Outcome data in the target population** (optional), for a control-group SD or event rate.
3. **Cited by the included reviews.** After screening, the reference lists of the reviews that
   passed (from Europe PMC, or PubMed Central when Europe PMC has none) are added, without the
   reviews, guidelines and letters they also cite, and screened. A trial that no query matched
   still comes in this way.

The **Search** tab shows each level's hits and how many of its records were found, kept after
screening and extracted.

| Step | Model | Effort |
|---|---|---|
| Read the problem | Claude Opus 5.5 (`claude-opus-5-5`) | high |
| Plan the search (tool use) | Claude Opus 5.5 | high |
| Screen titles and abstracts | Claude Haiku 5.5 (`claude-haiku-5-5`) | low |
| Download a PDF the browser can't (browser edition) | Claude Sonnet 5.5 (`claude-sonnet-5-5`) | low |
| Extract | Claude Haiku 5.5 | medium |
| Re-extract escalated papers | Claude Opus 5.5 | xhigh |

**Sonnet's download step.** A web page may only read files from sites that allow it (CORS), and
most publishers don't. The browser edition first tries each download itself; when the browser
refuses, Sonnet fetches the address with the web fetch tool on Anthropic's servers and the page
hands the document to Haiku. The fetch tool is limited to one fetch per call, so nothing inside a
fetched document can make Sonnet fetch anything else. Sonnet reads the whole PDF to fetch it, so
this step costs more per paper than Haiku's reading; the **Tokens used** panel shows it.

Opus calls opt into server-side refusal fallbacks (`fallbacks: "default"`), so a safety
classifier's false positive on a biomedical paper is retried on another model rather than lost.
Haiku has no server-side fallback; a paper Haiku declines goes to Opus.

### When Opus re-extracts a paper

- A quote is not in the source, or a number is not in its quote.
- A plausibility check fails: a p-value outside [0, 1], a CI lower bound above its upper bound, a
  non-positive ratio, events above n, SD and SE inconsistent with n, SDs differing more than
  threefold between groups (one may be an SE), or means reported without group sizes.
- Haiku reported low confidence or listed a concern (an SD that may be an SE, values read from a
  figure, a multi-arm trial, change scores).
- Haiku declined, ran out of tokens, or failed.
- The paper is in the random audit sample (10% of papers that passed everything, by default), which
  measures how often Haiku is wrong without tripping a check.

Nothing is dropped silently: a record that cannot be screened goes forward as "unsure", and a paper
neither model could extract shows up under **Needs attention**.

## Publish the browser edition on GitHub Pages

1. In the repository's **Settings → Pages**, set **Source** to **GitHub Actions**. Pages for a
   private repository needs a GitHub plan that includes it; the published page itself can be opened
   by anyone who has its address, which is fine here because nothing is stored on a server.
2. Merge to `main`. The **Pages** workflow builds `site/` (it bundles the Anthropic TypeScript SDK
   and pdf.js at pinned versions) and publishes it; the workflow run shows the address.
3. Share the address. Each person pastes their own Anthropic API key and, under **Contact email and
   NCBI key**, an email address (NCBI asks for one; Unpaywall needs it to find open-access PDFs).

Keep the tab open while a search runs; if it closes, open the search again and choose **Continue**.
Searches live in that browser only: another browser or computer starts empty, and clearing the
site's data deletes them. Export what you want to keep.

To try the page on your own computer:

```bash
cd site && npm ci && npm run build && cd ..
python3 -m http.server --directory site 8080   # then open http://localhost:8080
```

**Which sites accept requests from a web page** decides how often Sonnet is needed. To check from
your own browser, open https://example.com, press F12, and paste this into the Console:

```js
for (const u of [
  'https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=pubmed&term=mindfulness&retmode=json&retmax=1',
  'https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=mindfulness&format=json&pageSize=1',
  'https://clinicaltrials.gov/api/v2/studies?query.term=mindfulness&pageSize=1',
  'https://api.unpaywall.org/v2/10.1038/nature12373?email=you@example.com',
]) fetch(u).then(r => console.log('OK', r.status, u)).catch(() => console.log('BLOCKED', u));
```

A blocked search API still works through Sonnet, but its results then pass through the model as
tokens, and the web fetch tool accepts addresses of at most 250 characters, so long PubMed queries
fail there. If one of these turns out to be blocked, a small proxy for it is the better fix.

## Run the local server

Requirements: Python 3.10 or later and an Anthropic API key.

```bash
git clone https://github.com/hyungrok-do/effect-size-dev.git
cd effect-size-dev
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

export ESF_CONTACT_EMAIL=you@university.edu   # NCBI asks for it; Unpaywall requires it for PDFs
esf serve                                      # then open http://127.0.0.1:8000
```

Paste your Anthropic API key into the **Anthropic API key** box at the top of the page. The page
sends it only with the requests that run Claude (reading the problem, searching, extracting); the
server uses it for that job in memory and never writes it to disk. In the browser it is kept for
the tab, or on the browser if you tick **Remember on this browser**; **Forget** clears it.
Alternatively, set `ANTHROPIC_API_KEY` on the server, and the box becomes optional.

Searches are stored in `data/esf.sqlite3`, and the text read from each paper in `data/projects/`.

| Variable | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | | Optional server-side key, used when the page sends none. |
| `ESF_CONTACT_EMAIL` | | Sent to NCBI and Unpaywall. Without it, open-access PDFs are not fetched. |
| `NCBI_API_KEY` | | Optional; raises the PubMed rate limit from 3 to 10 requests a second. |
| `ESF_MANAGER_MODEL` | `claude-opus-5-5` | The manager model. |
| `ESF_WORKER_MODEL` | `claude-haiku-5-5` | The worker model. |
| `ESF_AUDIT_FRACTION` | `0.1` | Share of clean extractions that Opus re-checks anyway. |
| `ESF_PER_QUERY_LIMIT` | `100` | Records taken from each level's query, by relevance. |
| `ESF_MAX_CANDIDATES` | `300` | Records screened from the levels and registry queries. |
| `ESF_MAX_REVIEWS_FOLLOWED` | `10` | Included reviews whose references are added. |
| `ESF_MAX_CITED` | `150` | New records added from those references. |
| `ESF_MAX_EXTRACTIONS` | `60` | Papers extracted per search; the rest can be extracted one by one. |
| `ESF_WORKER_CONCURRENCY` | `8` | Parallel model calls. |
| `ESF_DATA_DIR` | `./data` | Where searches are stored. |

## The browser edition's page

Each step has its own tab: **Setup** (your Anthropic API key, a contact email and an optional NCBI
key), **Describe**, **What to look for**, **Search** and **Review**. The page opens at Setup until
it has a key. It moves to the next tab when a step finishes, unless you have switched tabs in the
meantime. Papers from PubMed and ClinicalTrials.gov records are counted and reviewed separately,
and each paper says how it was found (as a review, in the target population, cited by a review…).

The **Agent activity** panel shows what each agent is doing right now (Opus 5.5 managing, Sonnet
5.5 fetching, Haiku 5.5 screening and reading, and the page's own code searching and checking),
with its calls and tokens, and a timeline of every step it took, which you can filter by agent.
The timeline is saved with the search. On wide screens the panel sits beside the steps; on
narrower ones it opens over the page from **Agent activity** at the top.

## Reviewing

Each paper shows where its text came from (registry results, PMC full text, open-access PDF, or
abstract only), which model produced the extraction, why Opus reviewed it if it did, and a warning
when another record shares its trial registration number. Each result lists its values with their
quotes and the outcome of the checks; **Show source text** opens the text with the quotes
highlighted.

A decision on a result overrides one made for its whole paper. **CSV: included results** exports
one row per value of the results you included, with its quote, location, check outcome, concerns,
flags, and your decision.

## Privacy

Your paragraph and the text of the papers are sent to Anthropic's API. Literature requests go to
NCBI, ClinicalTrials.gov, Europe PMC, and Unpaywall, and, in the browser edition, to the sites that
host open-access PDFs. Nothing else leaves your browser (browser edition) or the machine running the
server (local server).

In the browser edition the API key goes from the page straight to Anthropic, with the header
Anthropic requires for calls from a browser. Anyone who can run scripts in the page could read it,
so use the page only from its published address and don't add third-party scripts to it. In the
local server edition the key travels from the page to the server in a request header; if you host
that server for others, serve it over HTTPS.

## Not yet

- Pooling the extracted values or converting them to a common effect size, and handing the result to
  [Sample Size Calculator](https://github.com/hyungrok-do/sample-size-calc).
- Paywalled full text: only open-access text, abstracts, and registry results are read.
- Numbers shown only in figures: figure captions are kept, figure images are not read.
- The Message Batches API (half price, slower) for screening large searches; screening uses direct
  parallel calls.
- Shared searches: the browser edition keeps each person's searches in their own browser.

## Development

```bash
pip install -e ".[dev]" playwright
(cd site && npm ci && npm run build)   # the browser edition's vendored libraries
python scripts/build_site_data.py      # after changing prompts or schemas in python/esf
pytest
```

The tests use recorded-format responses for the literature APIs and a mock HTTP transport for the
Claude API, so they need neither network access nor an API key. `tests/test_site.py` drives the
browser edition in Chromium with every request answered from the same fixtures, including a PDF host
that refuses web pages so the download goes through Sonnet; it is skipped if Playwright or the
built `site/vendor/` is missing. Set `ESF_CHROMIUM` to use a particular Chromium binary.
