# Effect Size Finder

Describe a study you are planning in a short paragraph. Effect Size Finder searches PubMed, Europe
PMC and ClinicalTrials.gov, reads the papers it can open, and extracts the statistics they report (group
sizes, means, SDs, event counts, CIs, hazard ratios, and so on), each with the sentence or table
row it came from. You review the papers one at a time, including or excluding each result; the lead
model checks the studies for overlap and summarizes, for each effect size your plan needs, what the
studies you kept report. The agents run three ways, chosen on the **Agents** page: the **Claude
API** (an Anthropic key), the **OpenAI API** (an OpenAI key), or an **OpenAI subscription** (your
ChatGPT Plus or Pro plan, no key; local edition only).

It **searches for relevant papers**, **extracts the reported numbers** and **summarizes them**. It
does not combine studies into an effect size or compute a sample size: those choices are yours.

This project was largely generated with Claude Code and is under validation.

## Two editions

| | Browser edition (`site/`) | Local server (`python/`, `web/`) |
|---|---|---|
| Runs | Entirely in the page; published on GitHub Pages | A Python server on your computer |
| Install | None: open the page | `pip install -e .` and `esf serve` |
| Agents | Claude API or OpenAI API, with your own key | Claude API, OpenAI API, or OpenAI subscription (Sign in with ChatGPT, no key) |
| API key | Each person pastes their own; it goes only to Anthropic or OpenAI | Pasted in the page, or set on the server; none with a subscription |
| Searches stored | In that person's browser (IndexedDB) | In `data/` on the server |
| PDFs the browser can't download | Fetched by Claude Sonnet 5.5 (web fetch); with OpenAI, the abstract is read and the PDF can be uploaded | Downloaded by the server |

Both give the models the same prompts, schemas and tools: `scripts/build_site_data.py` writes them
from the Python package into `site/lib/generated.js`. The browser edition's quote checks, JATS
reader and registry formatting are ports of the Python ones, and the tests require identical output.

## How it works

```
paragraph
  ▼
OPUS    Read the problem → structured specification (PICO parts, outcomes, effect sizes to find, rules, terms)
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
OPUS    Summarize: for each effect size the plan needs, the values that bear on it and how they compare
```

The steps above name Claude's models; with OpenAI, GPT-6.1 Sol is the manager and GPT-6 Luna the
worker (see [Claude or OpenAI](#claude-or-openai)). The models read and quote; they never compute or
convert a number. Every value is checked by code against the text it was read from, and in the
summary the manager refers to values by key rather than typing them.

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

## Agents: Claude API, OpenAI API or OpenAI subscription

The **Agents** page (the **Agents** button at the top of either page) sets how the agents run and
which model plays each role. It has three choices:

| | Claude API | OpenAI API | OpenAI subscription |
|---|---|---|---|
| You give | An Anthropic API key | An OpenAI API key | A ChatGPT sign-in (Plus or Pro) |
| Billed | Per use, to your Anthropic account | Per use, to your OpenAI account | Against your ChatGPT plan's usage |
| Editions | Both | Both | Local only |
| Manager | Opus 5.5 | GPT-6.1 Sol (`gpt-6.1-sol`) | GPT-6.1 Sol, or another model your plan offers |
| Workers | Haiku 5.5 | GPT-6 Luna (`gpt-6-luna`) | GPT-6 Luna, or another model your plan offers |
| Fetcher (browser edition) | Sonnet 5.5, web fetch | None | (local edition: the server downloads) |

Under **Who does what** each role has its model: Opus, Sonnet or Haiku 5.5 with the Claude API;
GPT-6.1 Sol or GPT-6 Luna with the OpenAI API; the models your plan lists with a subscription. Each
choice is kept for its provider, so switching back and forth keeps both. The steps, prompts, schemas
and checks are the same whichever runs them. The browser edition opens on the Agents page until it
has a key; Setup then keeps the contact email and NCBI key, with a line naming the agents.

OpenAI's models are called through the Responses API (`python/esf/openai_llm.py`,
`site/lib/openai.js`), with the same reasoning effort per step as Claude's (lowered to the nearest
one a model takes, when its plan says which). Structured outputs use strict JSON schemas (the same
schemas Claude gets), and the search agent's tools are strict function tools. Requests are not
stored at OpenAI (`store: false`) and are streamed; without storage, each turn of the search agent
sends the whole conversation back, its reasoning included in encrypted form. OpenAI has no
counterpart to Claude's server-side web fetch, so in the browser edition a PDF the page may not
download is not fetched: the abstract is read instead, and the card says so; upload the PDF to read
it. A PDF is sent as a file; if ChatGPT plan usage refuses files, its text layer is sent instead.
There is no server-side refusal fallback: a paper the worker declines goes to the manager, as with
Haiku.

### Sign in with ChatGPT (local edition)

In the local edition, choose **OpenAI subscription** on the Agents page and click **Sign in with ChatGPT**. ChatGPT opens in a new
tab; after you sign in and allow plan usage it returns to the server (`/auth/callback`), and the
models run on your ChatGPT plan instead of an API key. This needs a ChatGPT Plus or Pro plan, and
counts against the plan's usage; ChatGPT Settings shows it and lets you cap this app's share. The
page then lists the models your plan offers, to pick the manager and the workers.

OpenAI offers this self-serve to open-source apps that run on your own computer: ChatGPT returns to
`http://127.0.0.1:<port>/auth/callback` only. The GitHub Pages edition is a hosted page, so there
the subscription is shown but can't be chosen; use the OpenAI API with a key, or the local edition. Open the local page on the computer that runs the server (the server
refuses a sign-in started from another machine). The sign-in uses OAuth with PKCE and an OIDC nonce,
and the identity token is verified against OpenAI's signing keys (`python/esf/chatgpt.py`). Only the
app's stable host ID, registration (its issued client ID and verified account subject), and your choice of models are saved, in
`data/chatgpt.json`; the tokens stay in the server's memory, are renewed as they expire, and are gone
when the server stops, so you sign in again after a restart. **Sign out** revokes them; you can also
disconnect the app in ChatGPT Settings. Returning sign-ins must use the account that registered this app.

While a sign-in waits in the ChatGPT tab, **Cancel** drops it; starting it again keeps the first tab
valid. If the plan's share runs out, or the sign-in expires, during a search, the search stops and
says so rather than sending a request for every remaining record (the same goes for a refused or
out-of-credit API key).

Plan usage is a preview with its own rules for requests (no output-token cap or sampling settings,
the system prompt in `instructions`, function tools in a namespace), which the OpenAI calls here
follow. They are tested against a mock of the API, not the live service; if OpenAI rejects a request,
the error names the reason.

## Publish the browser edition on GitHub Pages

1. In the repository's **Settings → Pages**, set **Source** to **GitHub Actions**. Pages for a
   private repository needs a GitHub plan that includes it; the published page itself can be opened
   by anyone who has its address, which is fine here because nothing is stored on a server.
2. Merge to `main`. The **Pages** workflow builds `site/` (it bundles the Anthropic TypeScript SDK
   and pdf.js at pinned versions) and publishes it; the workflow run shows the address.
3. Share the address. On the **Agents** page each person chooses the Claude API or the OpenAI API
   and pastes their own key; under **Setup**, an email address (NCBI asks for one; Unpaywall needs it to find open-access PDFs).

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

Requirements: Python 3.10 or later, and an Anthropic API key or a ChatGPT Plus or Pro plan.

```bash
git clone https://github.com/hyungrok-do/effect-size-dev.git
cd effect-size-dev
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

export ESF_CONTACT_EMAIL=you@university.edu   # NCBI asks for it; Unpaywall requires it for PDFs
esf serve                                      # then open http://127.0.0.1:8000
```

Open **Agents** at the top of the page and choose how the agents run. With the **Claude API** or
the **OpenAI API**, paste the key; the page sends it only with the requests that run the agents
(reading the problem, searching, extracting), in its own header, and the server uses it for that
job in memory and never writes it to disk. In the browser it is kept for the tab, or on the browser
if you tick **Remember on this browser**; **Forget** clears it. Alternatively, set
`ANTHROPIC_API_KEY` or `OPENAI_API_KEY` on the server, and the box becomes optional. With an
**OpenAI subscription**, click **Sign in with ChatGPT** (see
[above](#sign-in-with-chatgpt-local-edition)).

Searches are stored in `data/esf.sqlite3`, and the text read from each paper in `data/projects/`.

| Variable | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | | Optional server-side key for the Claude API, used when the page sends none. |
| `OPENAI_API_KEY` | | Optional server-side key for the OpenAI API, used when the page sends none. |
| `ESF_CONTACT_EMAIL` | | Sent to NCBI and Unpaywall. Without it, open-access PDFs are not fetched. |
| `NCBI_API_KEY` | | Optional; raises the PubMed rate limit from 3 to 10 requests a second. |
| `ESF_MANAGER_MODEL` | `claude-opus-5-5` | The manager model. |
| `ESF_WORKER_MODEL` | `claude-haiku-5-5` | The worker model. |
| `ESF_OPENAI_MANAGER_MODEL` | `gpt-6.1-sol` | The manager model on a ChatGPT plan, until the page picks another. |
| `ESF_OPENAI_WORKER_MODEL` | `gpt-6-luna` | The worker model on a ChatGPT plan, until the page picks another. |
| `ESF_ALLOWED_HOSTS` | | Host names the server answers to besides its addresses and `localhost`, comma-separated, to serve it under a name of its own. |
| `ESF_AUDIT_FRACTION` | `0.1` | Share of clean extractions that Opus re-checks anyway. |
| `ESF_PER_QUERY_LIMIT` | `100` | Records taken from each level's query, by relevance. |
| `ESF_MAX_CANDIDATES` | `300` | Records screened from the levels and registry queries. |
| `ESF_MAX_REVIEWS_FOLLOWED` | `10` | Included reviews whose references are added. |
| `ESF_MAX_CITED` | `150` | New records added from those references. |
| `ESF_MAX_EXTRACTIONS` | `60` | Papers extracted per search; the rest can be extracted one by one. |
| `ESF_WORKER_CONCURRENCY` | `8` | Parallel model calls. |
| `ESF_DATA_DIR` | `./data` | Where searches are stored. |

## The browser edition's page

Each step has its own tab: **Setup** (a contact email and an optional NCBI key, with a line naming
the agents), **Describe**, **Validate**, **Search**, **Review** and **Summary**. The agents and their
key are on their own **Agents** page, which the page opens on until it has a key. **Validate** shows how Opus read your paragraph, as a structured specification: the
question in one sentence, anything worth deciding before the search, then one table per part: the
comparison (population with its condition, age and setting; intervention with its delivery, dose
and duration; the comparator and its kind; the design), the outcomes (role, measure, type, when),
the effect sizes to find (outcome, statistics, per arm or between groups, when, and what each is for),
the values your paragraph states, the screening rules (include or exclude, and what each is about)
and the search terms grouped by concept, which Opus starts its concept blocks from. Click any item to change it where it is (Enter or clicking
away keeps the change, Escape drops it; tables have **+ Add** and × for rows); each change is kept
with the search. Searches saved before the specification
was structured are converted when they are opened. It moves to the next tab when a step finishes, unless you have switched tabs in the
meantime. Papers from PubMed and ClinicalTrials.gov records are counted and reviewed separately,
and each paper says how it was found (as a review, in the target population, cited by a review…).

The **Search** tab opens with a **search map** of how the search works, which fills in as it runs:
each stage (plan, the levels from the top down, screening, the reviews' references, reading and
extracting, the overlap check) with the agents that do it, a line on its logic, whether it is done,
running (with how far it has got) or stopped, and what it found: each level's query, hits, records
taken and new records; how screening and extraction came out; the references followed. The local
server's page shows the same map.

The **Agent activity** panel lists every job each agent is doing right now (with Claude, Opus 5.5
managing, Sonnet 5.5 fetching, Haiku 5.5 screening and reading; with OpenAI, GPT-6.1 Sol and GPT-6
Luna; and the page's own code searching and checking): which paper or query, how long it has run and the tokens it has used so far, with the
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

Each paper is kept short and cited in full: its title, linked to its free full text in PubMed
Central when there is one (else its PubMed, Europe PMC or ClinicalTrials.gov page), with **Exclude
the whole paper** beside it; its key (R12, which the overlap check and the summary refer to), year,
venue and authors, which open a table of every author and their affiliations, one institution per
line, without the email addresses and footnote markers PubMed's affiliation strings carry (records
from before 2014 usually give the first author's affiliation only, and the table says so; for a
registry record, the investigators and their roles); its identifiers, each linked, free full text first (PMCID,
PMID, DOI, the Europe PMC ID of a record PubMed does not have, and trial registration numbers). A
record saved before authors were kept fetches them from its source when the table opens. Then a
warning only when there is something to watch (a preprint, abstract only, a result that differs from the plan,
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
the paper shows the note. When a study is reported more than once, the summary marks the other
reports' values as the same participants as the record to use; a review and the studies it pools
are separate studies and are not marked.

### The summary

**Write the summary** gives Opus the specification, the overlap check, the records you kept
something from, and every value of the results you included or left undecided (none you
excluded), each with a key (V1, V2, ...), its group size, how relevant it is and whether its quote
was found. Opus is told to describe, not decide: it does not combine studies, average or pool
values, convert between statistics, or recommend a value.

The summary has an overview of what was found and how close it is to the planned study; then, for
each effect size the plan needs, the values that bear on it side by side, one row per study arm with a
column per statistic (n, mean, SD, ...), whether each quote was found and which arms share
participants, with Opus's note on how they compare: which match the planned measure, timepoint and population, how widely they vary, and
which come from larger or better reported studies. Overlap, what to check before using the values,
and what the sources don't give come last, with a table of every value kept. Each study, in the
tables and wherever the text names it by key (R12), links to its PubMed, Europe PMC or
ClinicalTrials.gov page. Opus writes values'
keys rather than numbers (the SDs run from [V4] to [V9]), and the page shows the numbers as the
papers print them. If you change a decision afterwards, the page says the summary is out of date
until you write it again; so does re-extracting a paper. If writing it again fails, the previous
summary stays, with the error.

## Privacy

Your paragraph and the text of the papers are sent to Anthropic's API, or to OpenAI's if you choose
it. Literature requests go to
NCBI, ClinicalTrials.gov, Europe PMC, and Unpaywall, and, in the browser edition, to the sites that
host open-access PDFs. Nothing else leaves your browser (browser edition) or the machine running the
server (local server).

In the browser edition the API key goes from the page straight to Anthropic (with the header
Anthropic requires for calls from a browser) or to OpenAI. Anyone who can run scripts in the page could read it,
so use the page only from its published address and don't add third-party scripts to it. In the
local server edition the key travels from the page to the server in a request header (the
Anthropic key in `X-Anthropic-Api-Key`, the OpenAI key in `X-OpenAI-Api-Key`, only the chosen one); if
you host that server for others, serve it over HTTPS. The server answers only to its own address,
`localhost`, or a name in `ESF_ALLOWED_HOSTS`, accepts changes only from its own page, and may not be
shown in a frame on another site, so another web page open in the same browser can't drive it. A
ChatGPT sign-in can be used only from the computer that runs the server: another computer can't run
jobs on that plan, see who signed in, or sign them out.

Behind a reverse proxy, have the proxy pass the original `Host` (or list the public name in
`ESF_ALLOWED_HOSTS`) and `X-Forwarded-Proto`; for a proxy on another machine, uvicorn also needs
`FORWARDED_ALLOW_IPS` set to its address. Requests through a proxy count as coming from another
computer, so the ChatGPT sign-in can't be used that way: open the server's own address on its
computer for that, or use an API key.

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

The tests use recorded-format responses for the literature APIs and mock HTTP transports for the
Claude API, OpenAI's Responses API and ChatGPT's sign-in, so they need neither network access nor an
API key. `tests/test_site.py` drives the browser edition in Chromium with every request answered from
the same fixtures, including a PDF host that refuses web pages so the download goes through Sonnet,
and a search run on OpenAI's models; it is skipped if Playwright or the built `site/vendor/` is
missing. `tests/test_web.py` drives the local page against the server: signing in with ChatGPT (the
browser's trip to ChatGPT answered with its redirect back) and searching on the plan. Set `ESF_CHROMIUM` to use a particular Chromium binary.
