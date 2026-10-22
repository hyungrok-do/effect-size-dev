# Effect Size Finder

Describe a study you are planning in a short paragraph. Effect Size Finder searches PubMed, Europe
PMC and ClinicalTrials.gov, reads the papers it can open, and extracts the statistics they report (group
sizes, means, SDs, event counts, CIs, hazard ratios, and so on), each with the sentence or table
row it came from. You review the papers one at a time, including or excluding each result; Opus
checks the studies for overlap and summarizes, for each number your plan needs, what the studies
you kept report.

It **searches for relevant papers**, **extracts the reported numbers** and **summarizes them**. It
does not combine studies into an effect size or compute a sample size: those choices are yours.

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
OPUS    Read the problem → structured specification (PICO parts, outcomes, numbers to find, rules, terms)
YOU     Check and edit the specification
OPUS    Plan the search from the top down: concept blocks, hit counts, then one query per level
CODE    Run the levels in order (reviews → target population → full text → wider), then ClinicalTrials.gov
HAIKU   Screen every title and abstract: include / exclude / unsure
CODE    Add the studies cited by the reviews that passed screening; Haiku screens those too
CODE    Get text: posted registry results → PMC full text (Europe PMC) → open-access PDF (Unpaywall) → abstract
SONNET  (browser edition) Download what the browser may not: PDFs, and any site that refuses web pages
HAIKU   Extract every reported number with its verbatim quote and location
CODE    Check each quote is in the source and each number is in its quote; plausibility checks
OPUS    Re-extract anything that failed a check, was flagged, or fell in the random audit sample
OPUS    Check the extracted records for overlap: one study reported twice, reviews pooling studies in the set
YOU     Review one paper at a time: include or exclude each result; export CSV or JSON
OPUS    Summarize: for each number the plan needs, the values that bear on it and how they compare
```

Claude reads and quotes; it never computes or converts a number. Every value is checked by code
against the text it was read from, and in the summary Opus refers to values by key rather than
typing them.

### The search, from the top down

A search that ANDs every detail of the plan together finds almost nothing: abstracts often leave
out the instrument (PHQ-9) or the comparator. For a mindfulness app versus waitlist for PHQ-9 in
university students, PubMed returned 1 record for all of it together (October 2026), but 29 reviews
of mindfulness apps for depression, 32 trials of them in students and 216 in any population. So Opus builds the
search the way a librarian would:

1. **Concept blocks.** One block each for the intervention, the condition and the population, each
   a MeSH heading ORed with free-text synonyms. Opus checks each block's hit count (the
   `count_pubmed` tool) before combining them, and never makes a PubMed block from the comparator
   or an instrument name.
2. **Levels**, run in this order:
   - **Reviews and meta-analyses** of the intervention and condition, in any population. They give
     pooled effects and list the trials they include.
   - **Studies in the target population**: population, intervention and condition, with a filter
     for the planned design (trials, or cohort and case-control studies).
   - **Outcome reported in the full text**, searched in Europe PMC, which also searches the full
     text of open-access articles section by section: the intervention and condition, with the
     instrument named in the methods or results (`METHODS:"PHQ-9" OR RESULTS:"PHQ-9"`). It finds the
     papers whose abstracts never mention the instrument but whose tables hold its means and SDs,
     and these usually have open full text to read. Europe PMC also has articles only in PubMed Central and
     preprints (medRxiv, PsyArXiv and others), which the review marks as not peer reviewed. Records
     PubMed already found are not screened twice, and the screening step is told which records this
     search found, so an abstract that doesn't name the outcome isn't held against them.
   - **Studies in related populations**, the same as the target-population level with its narrowest
     concept relaxed, when the target population has few.
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
key), **Describe**, **What to look for**, **Search**, **Review** and **Summary**. The page opens at Setup until
it has a key. **What to look for** shows how Opus read your paragraph, as a structured specification: the
question in one sentence, anything worth deciding before the search, then one table per part: the
comparison (population with its condition, age and setting; intervention with its delivery, dose
and duration; the comparator and its kind; the design), the outcomes (role, measure, type, when),
the numbers to find (outcome, statistics, per arm or between groups, when, and what each is for),
the values your paragraph states, the screening rules (include or exclude, and what each is about)
and the search terms grouped by concept, which Opus starts its concept blocks from. Each part has
its own **Edit**, and an edit is kept with the search. Searches saved before the specification
was structured are converted when they are opened. It moves to the next tab when a step finishes, unless you have switched tabs in the
meantime. Papers from PubMed and ClinicalTrials.gov records are counted and reviewed separately,
and each paper says how it was found (as a review, in the target population, cited by a review…).

The **Search** tab opens with a **search map** of how the search works, which fills in as it runs:
each stage (plan, the levels from the top down, screening, the reviews' references, reading and
extracting, the overlap check) with the agents that do it, a line on its logic, whether it is done,
running (with how far it has got) or stopped, and what it found: each level's query, hits, records
taken and new records; how screening and extraction came out; the references followed. The local
server's page shows the same map.

The **Agent activity** panel lists every job each agent is doing right now (Opus 5.5 managing,
Sonnet 5.5 fetching, Haiku 5.5 screening and reading, and the page's own code searching and
checking): which paper or query, how long it has run and the tokens it has used so far, with the
searches Opus runs while planning shown under the planning job, and how far the current batch has
got (screening 14 of 60). An idle agent shows its last job and how it ended. Below is a timeline of
every finished job with its tokens and its outcome: the screening decision and reason, the numbers
an extraction found, the queries of the search plan. It is filtered by agent, shows the latest 250
jobs and the rest on request. This costs nothing extra: it comes from the calls the search makes
anyway. The timeline is saved with the search.

**Export log** downloads the whole log, oldest first, as CSV (a row per job) or JSON: when each job
started and finished, the agent and the models that served its calls, the step, the paper it was
about, what it was, its whole outcome or error, how long it took, and its calls and tokens. Each job
has an id, and the searches Opus runs while planning name the planning job as their parent. The
search's JSON export includes the log too. On wide screens the panel sits beside the steps; on
narrower ones it opens over the page from **Agent activity** at the top.

## Reviewing

The review shows one paper at a time. Above it, a progress line (reviewed 3 of 12 papers) and a
numbered chip per paper, colored once its results are decided, let you jump to any paper;
**Previous** and **Next** (or the arrow keys) step through them, and **Next not yet reviewed** skips
to the first paper still open. Next is highlighted once every result of the paper is decided.

Each paper is kept short and cited in full: its title, linked to its PubMed, Europe PMC or
ClinicalTrials.gov page; its key (R12, which the overlap check and the summary refer to), authors,
year and venue; its identifiers, each linked (PMID, PMCID, DOI, the Europe PMC ID of a record PubMed
does not have, and trial registration numbers); a warning only when there is something to watch (a preprint, abstract only, a result that differs from the plan,
an overlap with another record); and each result in a box with Include and Exclude. A result reads
as a line per group in the usual shorthand, as printed: `App  8.1 ± 4.2  n = 100`, `median 7 [4–11]`,
`12/100 events (12%)`, `MD -2.2 (95% CI -3.4 to -1) · p = 0.001`. A result is flagged only when a
quote did not check out, the match to the plan is partial, confidence is low, or the extraction
raised a concern (an SD that may be an SE). **Quotes** opens each value with its quote, location
and check; **Show source text** opens the text with the quotes highlighted; **Details** has how the
paper was found, the text used, which model extracted it and why, and screening notes.

A decision on a result overrides one made for its whole paper. **CSV: included results** exports
one row per value of the results you included, with its quote, location, check outcome, concerns,
flags, and your decision.

### Overlap

After extraction Opus looks across the records for participants that would be counted twice: a
registry record and its paper, a main paper and a secondary analysis (`same_study`); a systematic
review or meta-analysis whose pooled estimate includes a primary study also in the set
(`review_includes_study`); two reviews pooling some of the same studies (`reviews_share_studies`);
and overlaps it suspects but cannot confirm (`possible_overlap`). It is given what tells them
apart: registration numbers, authors, arms and their sizes, populations, dates, and which reviews
in the set cite which records (from the reference lists read during the search). A citation alone
is not taken as proof that a review pools a study. For each group Opus says whose numbers to use;
the paper shows the note, and the summary marks the other records' values as the same participants.

### The summary

**Write the summary** gives Opus the specification, the overlap check, the records you kept
something from, and every value of the results you included or left undecided (none you
excluded), each with a key (V1, V2, ...), its group size, how relevant it is and whether its quote
was found. Opus is told to describe, not decide: it does not combine studies, average or pool
values, convert between statistics, or recommend a value.

The summary has an overview of what was found and how close it is to the planned study; then, for
each number the plan needs, the values that bear on it side by side, one row per study arm with a
column per statistic (n, mean, SD, ...), whether each quote was found and which arms share
participants, with Opus's note on how they compare: which match the planned measure, timepoint and population, how widely they vary, and
which come from larger or better reported studies. Overlap, what to check before using the values,
and what the sources don't give come last, with a table of every value kept. Each study, in the
tables and wherever the text names it by key (R12), links to its PubMed, Europe PMC or
ClinicalTrials.gov page. Opus writes values'
keys rather than numbers (the SDs run from [V4] to [V9]), and the page shows the numbers as the
papers print them. If you change a decision afterwards, the page says the summary is out of date
until you write it again.

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

- Combining studies: converting values to a common effect size (a standardized mean difference from
  means and SDs, a risk ratio from event counts), pooling SDs or effects across studies, and
  handing the result to [Sample Size Calculator](https://github.com/hyungrok-do/sample-size-calc).
  The summary lays the values side by side for you to decide.
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
