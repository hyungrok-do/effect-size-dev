# Effect Size Finder

Describe a study you are planning in a short paragraph. Effect Size Finder searches PubMed and
ClinicalTrials.gov, reads the papers it can open, and extracts the statistics they report (group
sizes, means, SDs, event counts, CIs, hazard ratios, and so on), each with the sentence or table
row it came from. You review the results, include or exclude each one, and export what you keep.

This first version does two things: **search for relevant papers** and **extract the reported
numbers**. It does not pool them or compute a sample size yet.

This project was largely generated with Claude Code and is under validation.

## How it works

```
paragraph
  ▼
OPUS    Read the problem → search specification (PICO, design, outcome type, statistics needed)
YOU     Check and edit the specification
OPUS    Plan the search: explores PubMed and ClinicalTrials.gov with tools, then submits final queries
CODE    Run the queries, de-duplicate, fetch PubMed records
HAIKU   Screen every title and abstract: include / exclude / unsure
CODE    Get text: posted registry results → PMC full text (Europe PMC) → open-access PDF (Unpaywall) → abstract
HAIKU   Extract every reported number with its verbatim quote and location
CODE    Check each quote is in the source and each number is in its quote; plausibility checks
OPUS    Re-extract anything that failed a check, was flagged, or fell in the random audit sample
YOU     Include or exclude each result; export CSV or JSON
```

Claude reads and quotes; it never computes or converts a number. Every value is checked by code
against the text it was read from.

| Step | Model | Effort |
|---|---|---|
| Read the problem | Claude Opus 5.5 (`claude-opus-5-5`) | high |
| Plan the search (tool use) | Claude Opus 5.5 | high |
| Screen titles and abstracts | Claude Haiku 5.5 (`claude-haiku-5-5`) | low |
| Extract | Claude Haiku 5.5 | medium |
| Re-extract escalated papers | Claude Opus 5.5 | xhigh |

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

## Run it

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
| `ESF_PER_QUERY_LIMIT` | `100` | Records taken from each final query. |
| `ESF_MAX_CANDIDATES` | `300` | Records screened per search. |
| `ESF_MAX_EXTRACTIONS` | `60` | Papers extracted per search; the rest can be extracted one by one. |
| `ESF_WORKER_CONCURRENCY` | `8` | Parallel model calls. |
| `ESF_DATA_DIR` | `./data` | Where searches are stored. |

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
NCBI, ClinicalTrials.gov, Europe PMC, and Unpaywall. Everything else stays on the machine running
the server.

The API key travels from the page to the server in a request header. On your own computer that
stays local; if you host the server for others, serve it over HTTPS so keys are not sent in the
clear.

## Not yet

- Pooling the extracted values or converting them to a common effect size, and handing the result to
  [Sample Size Calculator](https://github.com/hyungrok-do/sample-size-calc).
- Paywalled full text: only open-access text, abstracts, and registry results are read.
- Numbers shown only in figures: figure captions are kept, figure images are not read.
- The Message Batches API (half price, slower) for screening large searches; screening uses direct
  parallel calls.
- Accounts and hosting: the server is meant to run locally, with your own API key.

## Development

```bash
pytest
```

The tests use recorded-format responses for the literature APIs and a mock HTTP transport for the
Claude API, so they need neither network access nor an API key.
