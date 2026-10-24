"""The browser edition (site/), driven in Chromium. Every request the page makes, to Anthropic and to
the literature APIs, is answered here from fixtures, so nothing leaves the machine. The PDF host
refuses requests from web pages, which sends that download through Sonnet's web fetch."""

from __future__ import annotations

import base64
import csv
import functools
import glob
import io
import json
import os
import re
import threading
import time
from dataclasses import asdict
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import httpx
import pytest

from esf import prompts
from esf.config import REPO_ROOT
from esf.jats import jats_to_text
from esf.sources import clean_affiliations, europepmc_pmid, europepmc_record, europepmc_url, parse_pubmed_xml, trial_results_text
from esf.evidence import build_evidence, decisions_basis, overlap_records
from esf.schemas import upgrade_spec
from esf.verify import in_audit_sample, numbers_in, verify
from fixtures import (
    ABSTRACT_LINE, EUROPEPMC, EUROPEPMC_QUERY, JATS, PDF_LINE, PLAN, PMC_ONLY, PREPRINT, PUBMED_XML, REVIEW_FILTER, SPEC, TRIAL,
    FakeLLM, FakeOpenAI, LiteratureAPIs, _extraction, _result, _value, make_pdf, streamed,
)

sync_api = pytest.importorskip("playwright.sync_api")
SITE = REPO_ROOT / "site"
if not (SITE / "vendor" / "anthropic-sdk.mjs").exists():
    pytest.skip("build the site first: cd site && npm ci && npm run build", allow_module_level=True)

KEY = "sk-ant-test-site"
OPENAI_KEY = "sk-proj-test-site-0123456789"

def _paper(pid, values, status="extracted", **fields):
    """A paper as the stores hold it, with one result per list in `values` (group, statistic, value)."""
    results = [{"outcome_measure": outcome, "timepoint": timepoint, "analysis_population": "itt", "adjusted": "unadjusted",
                "relevance": "direct", "confidence": "high", "concerns": [],
                "values": [{"group": g, "statistic": st, "value": v, "describes": f"{st} of {outcome}", "quote": "q", "location": "Table 1"}
                           for g, st, v in rows]}
               for outcome, timepoint, rows in values]
    checks = [[{"quote": "exact" if k % 2 == 0 else "missing", "number_in_quote": True, "number_in_source": True}
               for k, _ in enumerate(rows)] for _, _, rows in values]
    return {"id": pid, "status": status, "source": "pubmed", "pmid": str(1000 + pid), "nct_ids": [], "doi": None, "title": f"Trial {pid}",
            "authors": "Kim J, et al.", "year": "2020", "journal": "J", "pubtypes": ["Randomized Controlled Trial"], "cited_by": [],
            "extraction": {"study_design": "RCT", "population": "students", "groups": ["App (n = 50)", "Waitlist (n = 50)"],
                           "matches_spec": True, "mismatch_reasons": [], "results": results, "notes": ""},
            "verification": {"values": checks, "flags": []}, **fields}


EVIDENCE_PAPERS = [
    _paper(1, [("PHQ-9", "12 weeks", [("App", "n", 50), ("App", "mean", 8.1), ("App", "sd", 4.2), ("Waitlist", "n", 48),
                                     ("Waitlist", "mean", 10.3), ("Waitlist", "sd", 4.5)]),
               ("GAD-7", "", [("App", "mean", 6.0)])]),
    _paper(2, [("PHQ-9 ", "12 Weeks", [("Mindfulness", "n", 30), ("Mindfulness", "sd", 3.9), ("Control", "sd", 4.4)])],
           nct_ids=["NCT00000001"]),
    _paper(3, [("PHQ-9", "12 weeks", [("App", "mean", 9.0), ("App", "sd", 4.0), ("App", "n", 75)])], cited_by=["1004"]),
    _paper(4, [("Depression", "post", [("App vs control", "standardized_mean_difference", 0.35)])],
           pubtypes=["Meta-Analysis"], cited_by=["9999"]),
    _paper(5, [("PHQ-9", "12 weeks", [("App", "sd", 9.9), ("App", "n", 10)])], nct_ids=["NCT00000001"]),
    _paper(6, [("PHQ-9", "12 weeks", [("App", "sd", 1.0), ("App", "n", 5)])]),
    _paper(7, [("PHQ-9", "12 weeks", [("App", "sd", 2.0), ("App", "n", 5)])], status="screened_out"),
]
EVIDENCE_DECISIONS = {"1": {"1": {"decision": "exclude"}, "0": {"decision": "include"}}, "6": {"-1": {"decision": "exclude"}}}
EVIDENCE_OVERLAP = {"groups": [
    {"records": ["R2", "R5"], "relation": "same_study", "evidence": "NCT00000001", "use": "R2"},
    # A review and two studies it pools: separate studies, so none shares another's participants.
    {"records": ["R4", "R1", "R3"], "relation": "review_includes_study", "evidence": "R4 cites R1 and R3", "use": "R1"},
], "notes": ""}

# Affiliation strings as PubMed and Europe PMC give them, cleaned the same way by both editions.
AFFILIATION_CASES = [
    ["The Research Clinic, Aarhus University Hospital, Denmark. lonefjor@rm.dk"],
    ["Department of Psychiatry, Chongqing, China. Electronic address: luo@hospital.cqmu.edu.cn."],
    ["School of Nursing (E-mail: a.b@uni.edu)", "School of Nursing"],
    ["1 Dept A, Uni X; 2Dept B, Uni Y", "Dept A, Uni X"],
    ["aDepartment of Medicine, Uni Z", "3rd Department of Surgery", "4th Military Medical University", "3D Lab, X"],
    [None, "", " ; .", "Universidad Nacional de Córdoba,\u00a0Córdoba, Argentina."],
]

# A specification saved before the structured fields.
OLD_SPEC = {
    "question": "Q?", "population": "Students", "intervention": "App", "comparator": "Waitlist", "outcomes": ["PHQ-9", "GAD-7"],
    "timepoint": "12 weeks", "design": "parallel_rct", "outcome_type": "continuous", "statistics_needed": ["Mean and SD per arm"],
    "stated_values": [], "inclusion_criteria": ["RCTs"], "exclusion_criteria": [], "search_terms": ["mindfulness"], "open_questions": [],
}
PDF_URL = "https://example.org/rct2.pdf"
CORS = {"access-control-allow-origin": "*"}
PREFLIGHT = {**CORS, "access-control-allow-headers": "*", "access-control-allow-methods": "GET, POST, OPTIONS"}
OPUS, SONNET, HAIKU = "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"


class QuietHandler(SimpleHTTPRequestHandler):
    extensions_map = {**SimpleHTTPRequestHandler.extensions_map, ".mjs": "text/javascript", ".js": "text/javascript"}

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def site_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(QuietHandler, directory=str(SITE)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/"
    server.shutdown()


@pytest.fixture(scope="module")
def browser():
    executable = os.environ.get("ESF_CHROMIUM") or next(iter(sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux/chrome"))[::-1]), None)
    with sync_api.sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=executable)
        yield b
        b.close()


def message(model, content, stop_reason, **usage):
    return {
        "id": "msg_test", "type": "message", "role": "assistant", "model": model, "content": content,
        "stop_reason": stop_reason, "stop_sequence": None, "usage": {"input_tokens": 1000, "output_tokens": 50, **usage},
    }


class Network:
    """Answers the page's requests. Requests to hosts in `refuse_web_pages` fail the way a refused
    cross-site request does in a browser (a failed fetch); mocked responses bypass the browser's CORS
    check, so leaving out the header would not refuse anything."""

    def __init__(self, refuse_web_pages=("example.org",), count_delay=0.0):
        self.literature = LiteratureAPIs()
        # Seconds to hold PubMed count requests, so the page can be seen while an agent is at work.
        self.count_delay = count_delay
        self.refuse = set(refuse_web_pages)
        self.llm = FakeLLM()
        self.claude: list[tuple[dict, dict]] = []
        self.openai = FakeOpenAI()
        self.openai_calls: list[tuple[dict, dict]] = []
        self.openai_quota: int | None = None  # screenings the OpenAI account has credit for
        self.refused: list[str] = []

    def __call__(self, route, request):
        host = urlparse(request.url).hostname
        if host in ("127.0.0.1", "localhost"):
            return route.continue_()
        if request.method == "OPTIONS":
            return route.fulfill(status=204, headers=PREFLIGHT)
        if host == "api.anthropic.com":
            return self.anthropic(route, request)
        if host == "api.openai.com":
            return self.openai_api(route, request)
        if host in self.refuse:
            self.refused.append(request.url)
            return route.abort("failed")
        if self.count_delay and "esearch.fcgi" in request.url and "retmax=0" in request.url:
            time.sleep(self.count_delay)
        response = self.literature(httpx.Request(request.method, request.url, content=request.post_data_buffer or b""))
        headers = {**CORS, "content-type": response.headers.get("content-type", "application/octet-stream")}
        route.fulfill(status=response.status_code, headers=headers, body=response.content)

    def anthropic(self, route, request):
        headers = request.headers
        given = headers.get("x-api-key") or headers.get("authorization", "").removeprefix("Bearer ")
        if given != KEY:
            error = {"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}, "request_id": "req_test"}
            return route.fulfill(status=401, headers={**CORS, "content-type": "application/json", "request-id": "req_test"}, body=json.dumps(error))
        path = urlparse(request.url).path
        if request.method == "GET" and path.startswith("/v1/models/"):
            model = path.rsplit("/", 1)[1]
            info = {"type": "model", "id": model, "display_name": model.replace("claude-", "Claude ").replace("-5-5", " 5.5").title(), "created_at": "2026-01-01T00:00:00Z"}
            return route.fulfill(status=200, headers={**CORS, "content-type": "application/json"}, body=json.dumps(info))
        body = json.loads(request.post_data)
        self.claude.append((request.headers, body))
        if body.get("stream"):
            return route.fulfill(status=200, headers={**CORS, "content-type": "text/event-stream"},
                                 body=streamed(body["model"], self.structured(body)))
        tools = body.get("tools") or []
        reply = self.fetch(body) if any(t.get("type") == "web_fetch_20250910" for t in tools) else self.search_turn(body)
        route.fulfill(status=200, headers={**CORS, "content-type": "application/json"}, body=json.dumps(reply))

    def openai_api(self, route, request):
        if request.headers.get("authorization") != f"Bearer {OPENAI_KEY}":
            error = {"error": {"message": "Incorrect API key provided.", "type": "invalid_request_error", "code": "invalid_api_key"}}
            return route.fulfill(status=401, headers={**CORS, "content-type": "application/json", "x-request-id": "req_openai"}, body=json.dumps(error))
        path = urlparse(request.url).path
        if request.method == "GET" and path.startswith("/v1/models/"):
            model = path.rsplit("/", 1)[1]
            return route.fulfill(status=200, headers={**CORS, "content-type": "application/json"},
                                 body=json.dumps({"id": model, "object": "model", "owned_by": "openai"}))
        body = json.loads(request.post_data)
        self.openai_calls.append((request.headers, body))
        screened = [b for _, b in self.openai_calls if b.get("text", {}).get("format", {}).get("name") == "ScreenDecision"]
        if self.openai_quota is not None and len(screened) > self.openai_quota:
            error = {"error": {"message": "You exceeded your current quota.", "type": "insufficient_quota", "code": "insufficient_quota"}}
            return route.fulfill(status=429, headers={**CORS, "content-type": "application/json"}, body=json.dumps(error))
        route.fulfill(status=200, headers={**CORS, "content-type": "text/event-stream"}, body=self.openai.respond(body))

    def structured(self, body):
        system = body["system"][0]["text"]
        content = body["messages"][0]["content"]
        if system.startswith(prompts.SPEC):
            return SPEC.model_dump()
        if system.startswith(prompts.OVERLAP):
            return self.llm.check_overlap(None, json.loads(content[0]["text"].split("\n", 1)[1])).model_dump()
        if system.startswith(prompts.SUMMARIZE):
            data = json.loads(content[0]["text"].split("\n", 1)[1])
            return self.llm.summarize(SPEC, data["overlap"], data).model_dump()
        if system.startswith(prompts.SCREEN):
            return self.llm.screen(None, content).model_dump()
        if system.startswith(prompts.EXTRACT):
            return self.llm.extract(None, content[0]).model_dump()
        if system.startswith(prompts.ADJUDICATE):
            return self.llm.adjudicate(None, content[0], None, ["flag"]).model_dump()
        raise AssertionError(f"unexpected prompt: {system[:60]}")

    def search_turn(self, body):
        turns = len(body["messages"])
        if turns == 1:
            return message(OPUS, [
                {"type": "tool_use", "id": "t0", "name": "count_pubmed", "input": {"queries": ["mindfulness[tiab]", f"mindfulness[tiab] AND {REVIEW_FILTER}"]}},
                {"type": "tool_use", "id": "t1", "name": "search_pubmed", "input": {"query": "mindfulness[tiab] AND depress*[tiab]", "max_results": 5}},
                {"type": "tool_use", "id": "t2", "name": "search_clinicaltrials", "input": {"query": "mindfulness AND depression"}},
                {"type": "tool_use", "id": "t4", "name": "search_europepmc", "input": {"query": EUROPEPMC_QUERY, "max_results": 5}},
            ], "tool_use")
        if turns == 3:
            results = body["messages"][2]["content"]
            assert results[0]["content"] == f"#1 4 | mindfulness[tiab]\n#2 1 | mindfulness[tiab] AND {REVIEW_FILTER}"
            assert "Total hits: 4" in json.dumps(results) and "NCT01234567" in json.dumps(results)
            assert results[3]["content"].splitlines()[1:] == [
                "22222222 | 2019 | MED | research-article; journal article | Brief mindfulness training versus waitlist: a randomised trial.",
                f"{PMC_ONLY} | 2023 | PMC | research-article; journal article | Mindfulness app use and depressive symptoms in <i>nursing</i> students: a randomised trial",
                f"{PREPRINT} | 2024 | PPR | preprint | A smartphone mindfulness programme for depressive symptoms: a pilot randomised trial",
            ]
            plan = PLAN.model_dump()
            return message(OPUS, [{"type": "tool_use", "id": "t3", "name": "submit_search_plan", "input": plan}], "tool_use")
        return message(OPUS, [{"type": "text", "text": "Done."}], "end_turn")

    def fetch(self, body):
        url = re.search(r"Fetch (\S+) with", body["messages"][0]["content"]).group(1)
        if url == PDF_URL:
            result = {"type": "web_fetch_result", "url": url, "retrieved_at": "2026-10-21T00:00:00Z", "content": {
                "type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                "data": base64.b64encode(make_pdf(PDF_LINE)).decode()}}}
        else:
            result = {"type": "web_fetch_tool_result_error", "error_code": "url_not_accessible"}
        return message(SONNET, [
            {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_fetch", "input": {"url": url}},
            {"type": "web_fetch_tool_result", "tool_use_id": "srvtoolu_1", "content": result},
            {"type": "text", "text": "done"},
        ], "end_turn", server_tool_use={"web_fetch_requests": 1})


def open_paper(page, title):
    """The review shows one paper at a time: open the one whose title starts with `title`."""
    page.click(f"#queue button[title^='{title}']")
    page.wait_for_selector(f"#papers .paper h3:has-text('{title}')")
    return page.locator("#papers .paper")


def test_browser_ports_match_python(browser, site_url):
    page = browser.new_page()
    page.goto(site_url + "vendor/LICENSES.txt")  # any page on the site's origin, to import its modules
    numbers = ["−2.2 (−3.4 to −1.0)", "mean 8·1 (SD 4·2)", "n = 1,234 of 2,000", "p<.001; 10-20 weeks", "PHQ-9 at 12 weeks: 8.1%"]
    text = "Results: PHQ-9 at 12 weeks was 8.1 (SD 4.2, n = 50) with the app."
    quote = "PHQ-9 at 12 weeks was 8.1 (SD 4.2, n = 50)"
    cases = [
        (_extraction([_result([_value("App", "mean", 8.1, quote), _value("App", "n", 50, quote)])]), text, True),
        (_extraction([_result([_value("App", "mean", 8.3, quote), _value("App", "n", 50, "invented 50")], confidence="low")]), text, True),
        (_extraction([_result([
            _value("A", "n", 100, "q"), _value("A", "sd", 4.0, "q"), _value("A", "se", 4.0, "q"), _value("B", "n", 100, "q"),
            _value("B", "sd", 0.4, "q"), _value("A vs B", "ci_lower", 2.0, "q"), _value("A vs B", "ci_upper", 1.0, "q"),
            _value("A vs B", "p_value", 1.5, "q"), _value("A vs B", "hazard_ratio", 0, "q"),
        ])]), text, False),
        (_extraction([_result([_value("Mindfulness", "mean", 7.7, ABSTRACT_LINE)])]), ABSTRACT_LINE, True),
    ]
    keys = [f"project:{i}" for i in range(50)]
    g_cases = [0, 1.5, 100.0, 1234567, 0.00001, 0.0001, -2.2, 12.345678, 1e21]
    data = {
        "jats": JATS, "trial": TRIAL, "pubmed": PUBMED_XML, "numbers": numbers, "keys": keys, "g": g_cases,
        "europepmc": EUROPEPMC["resultList"]["result"][:3], "old_spec": OLD_SPEC,
        "evidence": [EVIDENCE_PAPERS, EVIDENCE_DECISIONS, EVIDENCE_OVERLAP],
        "verify": [[e.model_dump(), t, r] for e, t, r in cases],
        "affiliations": AFFILIATION_CASES,
    }
    result = page.evaluate("""async (data) => {
        const jats = await import('/lib/jats.js');
        const sources = await import('/lib/sources.js');
        const v = await import('/lib/verify.js');
        const panel = await import('/lib/spec-panel.js');
        const ev = await import('/lib/evidence.js');
        const affiliations = await import('/lib/affiliations.js');
        return {
            jats: jats.jatsToText(data.jats),
            trial: sources.trialResultsText(data.trial),
            pubmed: sources.parsePubmedXml(data.pubmed),
            numbers: data.numbers.map(v.numbersIn),
            verify: data.verify.map(([e, t, r]) => v.verify(e, t, r)),
            audit: await Promise.all(data.keys.map((k) => v.inAuditSample(k, 0.1))),
            g: data.g.map(v.g),
            upgraded: panel.upgradeSpec(data.old_spec),
            overlapRecords: ev.overlapRecords(data.evidence[0]),
            evidence: ev.buildEvidence(...data.evidence),
            basis: ev.decisionsBasis(data.evidence[0], data.evidence[1]),
            affiliations: data.affiliations.map((texts) => affiliations.cleanAffiliations(texts)),
            europepmc: data.europepmc.map((r) => [sources.europepmcPmid(r), sources.europepmcPmid(r) ? null : sources.europepmcRecord(r),
                                                    sources.europepmcUrl(String(r.id))]),
        };
    }""", data)
    assert result["jats"] == jats_to_text(JATS)
    assert result["trial"] == trial_results_text(TRIAL)
    assert result["pubmed"] == [asdict(r) for r in parse_pubmed_xml(PUBMED_XML)]
    assert result["numbers"] == [numbers_in(s) for s in numbers]
    assert result["verify"] == [verify(e, t, r).to_dict() for e, t, r in cases]
    assert result["audit"] == [in_audit_sample(k, 0.1) for k in keys]
    assert result["g"] == [format(x, "g") for x in g_cases]
    assert result["upgraded"] == upgrade_spec(OLD_SPEC)
    assert result["overlapRecords"] == overlap_records(EVIDENCE_PAPERS)
    assert result["evidence"] == build_evidence(EVIDENCE_PAPERS, EVIDENCE_DECISIONS, EVIDENCE_OVERLAP)
    assert result["basis"] == decisions_basis(EVIDENCE_PAPERS, EVIDENCE_DECISIONS)
    assert result["affiliations"] == [clean_affiliations(texts) for texts in AFFILIATION_CASES]
    # What the evidence holds: the excluded result and paper are gone, the overlapping record is kept
    # and marked as the same participants as the record to use, and nothing is combined.
    evidence = result["evidence"]
    assert set(evidence) == {"records", "values"}
    assert not any(v["outcome"] == "GAD-7" for v in evidence["values"]) and not any(v["record"] == "R6" for v in evidence["values"])
    assert [r["key"] for r in evidence["records"]] == sorted({v["record"] for v in evidence["values"]}, key=lambda k: int(k[1:]))
    assert all(v["same_participants_as"] == ("R2" if v["record"] == "R5" else None) for v in evidence["values"])
    assert [v["key"] for v in evidence["values"]] == [f"V{i}" for i in range(1, len(evidence["values"]) + 1)]
    assert result["overlapRecords"][2]["cited_by"] == ["R4"] and result["overlapRecords"][3]["cited_by"] == []
    assert result["europepmc"] == [
        [europepmc_pmid(r), None if europepmc_pmid(r) else asdict(europepmc_record(r)), europepmc_url(str(r["id"]))]
        for r in data["europepmc"]
    ]


def test_results_read_as_a_line_per_group(browser, site_url):
    """The review shows each group's numbers in the usual shorthand, as printed, with n beside them."""
    page = browser.new_page()
    page.goto(site_url)
    v = lambda group, statistic, value: {"group": group, "statistic": statistic, "value": value}  # noqa: E731
    cases = [
        [v("App", "n", 100), v("App", "mean", 8.1), v("App", "sd", 4.2)],
        [v("A", "mean", -1.5), v("A", "se", 0.4)],
        [v("A vs B", "mean_difference", -2.2), v("A vs B", "ci_lower", -3.4), v("A vs B", "ci_upper", -1.0), v("A vs B", "p_value", 0.001)],
        [v("A", "events", 12), v("A", "n", 100), v("A", "percent", 12)],
        [v("A", "median", 7), v("A", "q1", 4), v("A", "q3", 11), v("A", "n", 30)],
        [v("A vs B", "hazard_ratio", 0.71), v("A vs B", "ci_lower", 0.55), v("A vs B", "ci_upper", 0.92)],
        [v("A", "mean_change", -3.1), v("A", "sd_change", 5.0), v("A", "icc", 0.05)],
        [v("A", "n", 98), v("A", "n", 100), v("A", "other", 3)],
        # A CI goes with the estimate it follows.
        [v("A vs B", "standardized_mean_difference", 0.45), v("A vs B", "ci_lower", 0.1), v("A vs B", "ci_upper", 0.8),
         v("A vs B", "mean_difference", -3.2), v("A vs B", "ci_lower", -5.1), v("A vs B", "ci_upper", -1.3)],
        [v("A", "mean", 25.1), v("A", "mean_change", -3.2), v("A", "ci_lower", -4.5), v("A", "ci_upper", -1.9)],
        [v("A", "mean", 8.1), v("A", "sd", 4.2), v("A", "ci_lower", 7.3), v("A", "ci_upper", 8.9)],
        [v("A", "ci_lower", 1), v("A", "ci_upper", 2)],
    ]
    lines = page.evaluate("""async (cases) => {
        const { groupLine } = await import('/lib/review-view.js');
        return cases.map((values) => groupLine(values));
    }""", cases)
    assert lines == [
        {"text": "8.1 ± 4.2", "n": 100},
        {"text": "-1.5 ± 0.4 (SE)", "n": None},
        {"text": "MD -2.2 (95% CI -3.4 to -1) · p = 0.001", "n": None},
        {"text": "12/100 events (12%)", "n": 100},
        {"text": "median 7 [4–11]", "n": 30},
        {"text": "HR 0.71 (95% CI 0.55 to 0.92)", "n": None},
        {"text": "change -3.1 ± 5 · ICC 0.05", "n": None},
        {"text": "n 100 · Other 3", "n": 98},
        {"text": "MD -3.2 (95% CI -5.1 to -1.3) · SMD 0.45 (95% CI 0.1 to 0.8)", "n": None},
        {"text": "mean 25.1 · change -3.2 (95% CI -4.5 to -1.9)", "n": None},
        {"text": "8.1 ± 4.2 (95% CI 7.3 to 8.9)", "n": None},
        {"text": "95% CI 1 to 2", "n": None},
    ], lines
    # The search map's stages for searches in each state.
    plan = {"levels": [{"level": "primary", "query": "q"}], "clinicaltrials_queries": [], "extra_ids": []}
    states = page.evaluate("""async (cases) => {
        const { stageStates } = await import('/lib/search-map.js');
        return cases.map(([project, running]) => stageStates(project, running));
    }""", [
        [{"status": "searching", "progress": {"stage": "done"}, "search_plan": None}, True],  # Search again, before it writes progress
        [{"status": "spec_ready", "progress": {"stage": "done"}, "search_plan": plan}, False],  # a finished search, spec edited
        [{"status": "screening", "progress": {"stage": "screening", "done": 3, "total": 9}, "search_plan": plan,
          "search_log": [{"level": "cited"}]}, True],  # screening what the reviews cite
        [{"status": "interrupted", "progress": {"stage": "extracting", "done": 1, "total": 4}, "search_plan": plan}, False],
    ])
    assert [s["plan"] for s in states] == ["active", "done", "done", "done"]
    assert set(states[1].values()) == {"done"} and states[0]["ready"] == "pending"
    assert (states[2]["follow"], states[2]["screen"], states[2]["extract"]) == ("done", "active", "pending")
    assert (states[3]["extract"], states[3]["overlap"]) == ("stopped", "pending")
    # An older record: PubMed gives the first author's affiliation only, with an email address. The
    # table shows the institution and says why the others have none.
    note = page.evaluate("""async () => {
        const { paperHeader } = await import('/lib/review-view.js');
        const paper = { id: 9, source: 'pubmed', ext_id: '23272986', title: 'T', authors: 'Fjorback LO, Arendt M',
          author_list: [{ name: 'Lone Overby Fjorback', affiliation: 'Aarhus University Hospital, Denmark. lonefjor@rm.dk' },
                        { name: 'Mikkel Arendt', affiliation: '' }] };
        const article = document.createElement('article');
        article.className = 'paper';
        article.append(...paperHeader(paper, {}).filter(Boolean));
        document.body.append(article);
        article.querySelector('.authors-btn').click();
        await new Promise((r) => setTimeout(r, 50));
        const pop = document.querySelector('.authors-pop:popover-open');
        return [pop.querySelector('tbody tr td:last-child').textContent, pop.querySelector('p.muted')?.textContent];
    }""")
    assert note == ["Aarhus University Hospital, Denmark.",
                    "PubMed gives an affiliation for the first author only, as most journals reported them before 2014."], note
    page.close()


def test_a_blocked_api_is_fetched_through_claude(browser, site_url):
    page = browser.new_page()
    page.goto(site_url + "vendor/LICENSES.txt")
    result = page.evaluate("""async () => {
        const { Http } = await import('/lib/sources.js');
        const asked = [];
        const http = new Http({
            fetchImpl: () => Promise.reject(new TypeError('Failed to fetch')),
            proxy: async (url) => { asked.push(url); return { kind: 'text', text: '{"ok": true}' }; },
        });
        const first = await http.get('https://blocked.example/api?x=1');
        const second = await http.get('https://blocked.example/api?x=2');
        let tooLong = null;
        try { await http.get('https://blocked.example/' + 'a'.repeat(300)); } catch (e) { tooLong = e.message; }
        return { first, second, asked, blocked: http.isBlocked('https://blocked.example/'), tooLong };
    }""")
    assert result["first"] == result["second"] == {"ok": True}
    assert result["asked"] == ["https://blocked.example/api?x=1", "https://blocked.example/api?x=2"]
    assert result["blocked"] is True
    assert "too long to fetch through Claude" in result["tooLong"]


def test_search_and_review_in_the_browser(browser, site_url, tmp_path):
    network = Network(count_delay=0.6)
    context = browser.new_context(viewport={"width": 1280, "height": 900}, accept_downloads=True)
    context.route("**/*", network)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    # Expected failed loads: the refused PDF, and 404s from Europe PMC and Unpaywall, which is how they say
    # they have no full text or PDF for a paper. Any other error is a bug.
    def expected(m):
        url = m.location.get("url", "")
        missing = "status of 404" in m.text and url.startswith(("https://www.ebi.ac.uk/", "https://api.unpaywall.org/"))
        return url.startswith("https://example.org/") or missing

    page.on("console", lambda m: errors.append(f"{m.text} ({m.location.get('url')})") if m.type == "error" and not expected(m) else None)

    page.goto(site_url)
    # Without a key the page opens on the Agents page, and a job sent off without one comes back to it.
    assert page.is_visible("#agents-page") and page.is_hidden(".layout") and page.is_hidden(".intro")
    assert page.get_attribute("#agents-toggle", "data-missing") == "true"
    assert page.locator(".provider-card .pc-title").all_inner_texts() == ["Claude API", "OpenAI API", "OpenAI subscription"]
    assert page.is_checked("input[name='provider'][value='anthropic']")
    # The subscription runs only in the local edition; here it says so and can't be chosen.
    assert page.is_disabled("input[name='provider'][value='chatgpt']")
    assert "local edition only" in page.inner_text(".provider-card[data-provider='chatgpt']")
    page.click("#agents-back")
    assert page.is_hidden("#agents-page") and page.get_attribute("#tab-setup", "aria-selected") == "true"
    page.click("#tab-problem")
    page.fill("#paragraph", "We propose a two-arm randomized trial of a 12-week mindfulness app versus waitlist for PHQ-9 in students.")
    page.click("#read")
    assert page.is_visible("#agents-page") and page.get_attribute(".keybox", "data-missing") == "true"
    page.fill("#api-key", KEY)
    assert page.get_attribute("#agents-toggle", "data-missing") == "false"
    assert page.locator("#roles li strong").all_inner_texts()[0] == "Opus 5.5 · manager"
    assert page.locator("#roles select").evaluate_all("els => els.map((e) => e.value)") == [OPUS, SONNET, HAIKU]
    page.click("#agents-done")
    page.click("#tab-setup")
    assert page.inner_text("#agents-summary") == ("Claude API: Opus 5.5 plans and reviews, Sonnet 5.5 fetches, "
                                                  "Haiku 5.5 screens and reads.")
    page.fill("#contact-email", "test@example.com")
    page.dispatch_event("#contact-email", "change")
    page.click("#setup-done")
    assert page.get_attribute("#tab-problem", "aria-selected") == "true"
    page.click("#read")
    page.wait_for_selector("#spec:not([hidden])")
    # Validate: the specification as structured tables, each item changed by clicking it.
    assert page.inner_text("#tab-spec").endswith("Validate")
    assert page.inner_text("#spec-question") == SPEC.question
    comparison = page.locator("[data-part='comparison']")
    assert "University students" in comparison.inner_text() and "Setting Universities" in " ".join(comparison.inner_text().split())
    numbers = page.locator("[data-part='numbers'] tbody tr").first.locator("td").all_inner_texts()
    assert numbers == ["Depressive symptoms", "Mean · SD · n", "Each arm", "12 weeks", "The difference and its SD", "×"], numbers
    assert page.locator("#spec-panel .edit-box").count() == 0  # no Edit buttons: the items themselves are clicked
    comparison.locator("[data-edit='population.description']").click()
    assert page.input_value("#spec-population-description") == "University students"
    page.fill("#spec-population-description", "Undergraduate students")
    page.press("#spec-population-description", "Enter")  # keeps the change; doesn't start the search
    comparison.get_by_text("Undergraduate students").wait_for()
    assert page.locator("#spec-population-description").count() == 0
    assert page.is_visible("#spec") and page.get_attribute("#tab-spec", "aria-selected") == "true"
    page.wait_for_function("""async () => {
        const { Store } = await import('/lib/store.js');
        return (await new Store().project(location.hash.slice(1))).spec.population.description === 'Undergraduate students';
    }""")
    # Escape drops a change; a choice is a select, kept as soon as it is chosen.
    comparison.locator("[data-edit='intervention.delivery']").click()
    page.fill("#spec-intervention-delivery", "Website")
    page.press("#spec-intervention-delivery", "Escape")
    assert "Smartphone app" in comparison.inner_text() and "Website" not in comparison.inner_text()
    comparison.locator("[data-edit='design']").click()
    page.select_option("#spec-design", "cluster_rct")
    comparison.get_by_text("Cluster randomized trial").wait_for()
    comparison.locator("[data-edit='design']").click()
    page.select_option("#spec-design", "parallel_rct")
    comparison.get_by_text("Parallel-group randomized trial").wait_for()
    # A statistic the tool doesn't know keeps its field open until it is fixed.
    page.locator("[data-edit='numbers.0.statistics']").click()
    find = page.locator("#spec-numbers-0-statistics")
    find.fill("Mean, SD, n, wobble")
    find.press("Enter")
    assert find.is_visible() and page.get_attribute("[data-part='numbers']", "data-editing") == "true"
    find.fill("Mean, SD, n, Mean difference")
    find.press("Enter")
    page.locator("[data-part='numbers']").get_by_text("Mean · SD · n · Mean difference").wait_for()
    # Rows: + Add opens the new row's first field; × removes a row; a row added and left empty is dropped.
    outcomes = page.locator("[data-part='outcomes']")
    outcomes.locator(".add-row").click()
    page.fill("#spec-outcomes-1-name", "Anxiety")
    page.press("#spec-outcomes-1-name", "Enter")
    assert outcomes.locator("tbody tr").count() == 2 and "Anxiety" in outcomes.inner_text()
    outcomes.locator("[aria-label='Remove outcome 2']").click()
    assert outcomes.locator("tbody tr").count() == 1
    outcomes.locator(".add-row").click()
    page.press("#spec-outcomes-1-name", "Escape")
    assert outcomes.locator("tbody tr").count() == 1
    page.click("[data-part='screening'] summary")
    assert "Animal studies" in page.inner_text("[data-part='screening']")
    assert "1 to include · 1 to exclude" in page.inner_text("[data-part='screening'] summary")
    page.click("#run")
    # While Opus (the manager) plans, the searches it runs show as its jobs, under the planning job, as they happen.
    page.wait_for_selector("#agents [data-agent='manager'] .job[data-depth='1']", timeout=15000)
    opus_jobs = page.locator("#agents [data-agent='manager'] .job .what").all_inner_texts()
    assert opus_jobs[0] == "Planning the search" and opus_jobs[1].startswith("Counts 2 PubMed queries"), opus_jobs
    assert "at work" in page.inner_text("#agents [data-agent='manager'] .state")
    # The search map shows Opus planning, and every later stage still to come.
    states = page.evaluate("[...document.querySelectorAll('#map-body .map-stage')].map((li) => [li.dataset.stage, li.dataset.state])")
    assert states == [["plan", "active"], ["search", "pending"], ["screen", "pending"], ["follow", "pending"],
                      ["extract", "pending"], ["overlap", "pending"], ["ready", "pending"]], states
    # The page shows the search while it runs and moves on to the review when it finishes (it only
    # does that when the search tab is the one open, so this also shows the search tab was followed).
    page.wait_for_selector("#tab-review[aria-selected='true']", timeout=30000)
    page.wait_for_selector("#review:not([hidden]) .paper")
    assert page.is_hidden("#progress-card") and page.is_hidden("#spec") and page.is_hidden("#problem") and page.is_hidden("#setup")

    # Papers and registry records are reviewed separately.
    sources = page.locator("#source-switch button").all_inner_texts()
    assert sources == ["Papers · 7 extracted of 8", "ClinicalTrials.gov · 1 extracted of 1"], sources
    tabs = page.locator("#tabs button").all_inner_texts()
    assert tabs[0] == "Extracted (7)" and tabs[3] == "Screened out (1)", tabs
    # One paper at a time: where you are, how far the review has got, and a chip per paper.
    assert page.inner_text("#review-position") == "Paper 1 of 7"
    assert page.inner_text("#review-progress-text") == "Reviewed 0 of 7 papers"
    assert page.locator("#queue button").count() == 7 and page.locator("#papers .paper").count() == 1
    page.keyboard.press("ArrowRight")
    assert page.inner_text("#review-position") == "Paper 2 of 7"
    # Papers only Europe PMC has: one from PubMed Central, and a preprint marked as one.
    pmc = open_paper(page, "Mindfulness app use and depressive symptoms")
    assert pmc.locator(".badge", has_text="Europe PMC").count() == 1 and pmc.locator(".badge", has_text="Full-text search").count() == 1
    assert pmc.locator("h3 a").get_attribute("href") == f"https://pmc.ncbi.nlm.nih.gov/articles/{PMC_ONLY}/"  # free full text first
    assert [" ".join(x.split()) for x in pmc.locator(".ids .id").all_inner_texts()] == [f"PMCID {PMC_ONLY} free full text"]
    assert pmc.locator(".meta").first.inner_text().startswith("R") and "Ahmed N, Brown T, Clark R, et al." in pmc.inner_text()
    preprint = open_paper(page, "A smartphone mindfulness programme")
    assert preprint.locator(".badge", has_text="Preprint, not peer reviewed").count() == 1
    assert preprint.locator(".ids .id").first.inner_text().startswith("DOI 10.1101/") and "Europe PMC PPR" in preprint.locator(".ids").inner_text()
    # Each paper says how the search found it, and what the overlap check found about it.
    assert open_paper(page, "Smartphone mindfulness").locator(".badge", has_text="Cited by a review").count() == 1
    review = open_paper(page, "Mindfulness apps for depressive symptoms: a systematic review")
    assert review.locator(".badge", has_text="Reviews search").count() == 1
    assert "A review and the studies it pools" in review.locator(".overlap-note").inner_text()
    trial = open_paper(page, "A mindfulness app for depressive")
    assert "Same study as" in trial.locator(".overlap-note").inner_text() and "Use this record’s numbers." in trial.locator(".overlap-note").inner_text()
    page.click("#source-switch button:has-text('ClinicalTrials.gov')")
    registry = page.locator("#papers .paper")
    assert registry.inner_text().startswith("A Randomized Trial of a Mindfulness App")
    assert "instead." in registry.locator(".overlap-note").inner_text()
    # A registry record lists its investigators, with their roles.
    registry.locator(".authors-btn").click()
    officials = page.locator(".authors-pop:popover-open")
    officials.locator("tbody tr").first.wait_for()
    assert officials.locator("strong").inner_text() == "Investigators (1)"
    assert [c.strip() for c in officials.locator("tbody tr").first.inner_text().split("\t")] == [
        "1", "Jane Doe, MD", "Principal investigator", "University of Example"]
    page.keyboard.press("Escape")
    page.click("#source-switch button:has-text('Papers')")

    # The search, level by level from the top down: found, kept after screening, extracted.
    page.click("#tab-progress")
    rows = page.evaluate("""[...document.querySelectorAll('#funnel tbody tr')].map((tr) => tr.dataset.level
        ? [tr.dataset.level, ...[...tr.querySelectorAll('td.num')].map((td) => Number(td.textContent))]
        : [tr.textContent])""")
    assert rows == [["Papers"], ["reviews", 1, 1, 1], ["primary", 4, 3, 3], ["full_text", 3, 3, 3], ["cited", 1, 1, 1],
                    ["ClinicalTrials.gov"], ["registry", 1, 1, 1]], rows
    funnel = page.inner_text("#funnel")
    assert "1 hit; the top 1 taken" in funnel and "4 hits; the top 4 taken" in funnel and "4 hits; the top 3 taken" in funnel
    assert "References of 1 review: 3 in PubMed, 1 already found, 1 not studies" in funnel
    # The search map: every stage done, each level with what its search returned, and the outcomes.
    stages = page.locator("#map-body .map-stage")
    assert stages.count() == 7 and page.locator("#map-body .map-stage[data-state='done']").count() == 7
    levels = page.evaluate("""[...document.querySelectorAll('#map-body .map-level')].map((li) =>
        [li.dataset.level, li.dataset.state, li.querySelector('.map-result').textContent])""")
    assert levels == [["reviews", "done", "1 hit → top 1 taken → 1 new"], ["primary", "done", "4 hits → top 4 taken → 4 new"],
                      ["full_text", "done", "4 hits → top 3 taken → 2 new"], ["registry", "done", "1 hit → top 1 taken → 1 new"]], levels
    screen = page.inner_text("#map-body [data-stage='screen']")
    assert "Included 8" in screen and "Excluded 1" in screen and "1 review among those included" in screen
    assert "1 review followed" in page.inner_text("#map-body [data-stage='follow']")
    extract = page.inner_text("#map-body [data-stage='extract']")
    assert "Extracted 8" in extract and "downloaded by Sonnet" in extract and "re-extracted by Opus" in extract
    assert "1 study reported more than once" in page.inner_text("#map-body [data-stage='overlap']")
    assert [x.get_attribute("data-agent") for x in page.locator("#map-body [data-stage='extract'] .map-agent").all()] == [
        "code", "fetcher", "worker", "manager"]
    assert page.locator("#map-body [data-stage='extract'] .map-agent").all_inner_texts() == ["Code", "Sonnet 5.5", "Haiku 5.5", "Opus 5.5"]
    page.click("#plan-details summary")
    strategy = page.locator("#plan-queries li").all_inner_texts()
    assert strategy[0].startswith("Reviews and meta-analyses · Pooled effects.") and strategy[1].startswith("Studies in the target population")
    page.click("#tab-review")

    # The activity panel: what each agent did, newest first.
    assert page.is_visible("#activity")
    agents = page.locator("#agents .agent").all_inner_texts()
    names = [" ".join(x.split()) for x in page.locator("#agents .agent h3").all_inner_texts()]
    assert names == ["Opus 5.5 manager", "Sonnet 5.5 fetcher", "Haiku 5.5 workers", "Code search and checks"], names
    assert "calls" in agents[0] and "Idle" in agents[0]
    # An idle agent says what it did last; the timeline gives each job's tokens and its full outcome.
    assert "Last job · " in agents[0] and "Checking 8 records for overlap" in agents[0] and "2 overlaps" in agents[0]
    assert "Extracting “" in agents[2]
    timeline_text = page.evaluate("document.querySelector('#timeline').textContent")
    assert "1k in · 50 out" in timeline_text
    assert "A mindfulness app for depressive symptoms in university students" in timeline_text
    assert "PHQ-9 at 12 weeks: App n 100, Waitlist n 100, App mean 8.1, App sd 4.2" in timeline_text
    timeline = "\n".join(page.locator("#timeline li").all_inner_texts())
    for expected in ("Reading the problem", "Counts 2 PubMed queries", "#1 4, #2 1", "Tries PubMed: mindfulness", "Planning the search",
                     "Final plan: primary → reviews → full_text", "Reviews and meta-analyses: mindfulness", "Collected 8 records to screen",
                     "5 papers from PubMed, 2 only in Europe PMC, 1 registry record from ClinicalTrials.gov",
                     "Tries Europe PMC: mindfulness", "Outcome reported in the full text (Europe PMC): mindfulness",
                     "Following the references of 1 included review", "References of “Mindfulness apps for depressive",
                     "Added 1 study cited by the reviews", "Screening “Smartphone mindfulness",
                     "Screening “Stress responses in a mouse model", "exclude (other): Animal study.", "include (review): Relevant review.",
                     "Fetching example.org/rct2.pdf for the browser", "PDF, 1 KB",
                     "Extracting “Brief mindfulness training", "Reviewing “Mindfulness groups", "Search finished"):
        assert expected in timeline, expected
    page.click("#activity-filter button:has-text('Sonnet')")
    assert page.locator("#timeline li .who").all_inner_texts() == ["Sonnet 5.5"]
    page.click("#activity-filter button:has-text('All')")

    assert network.refused == [PDF_URL]  # tried once directly, then fetched through Claude
    pdf_paper = open_paper(page, "Brief mindfulness training")
    assert pdf_paper.locator(".badge", has_text="Fetched by Sonnet").count() == 1
    assert pdf_paper.locator(".badge", has_text="Open-access PDF").count() == 1
    assert pdf_paper.locator(".badge", has_text="quote found").count() == 4
    assert open_paper(page, "Mindfulness groups and depressive").locator(".badge", has_text="Reviewed by Opus").count() == 1

    # A result reads as a line per group, mean ± SD with n; the quotes are folded away until asked for.
    trial = open_paper(page, "A mindfulness app for depressive")
    arms = trial.locator(".result").first.locator(".arms tr").all_inner_texts()
    assert [" ".join(a.split()) for a in arms][:2] == ["App 8.1 ± 4.2 n = 100", "Waitlist 10.3 ± 4.5 n = 100"], arms
    assert not trial.locator(".result details.quotes table").first.is_visible()
    # Its free full text in PubMed Central comes first: the title links there, and its PMCID leads.
    assert trial.locator("h3 a").get_attribute("href") == "https://pmc.ncbi.nlm.nih.gov/articles/PMC9000001/"
    # It is cited in full for credibility: year, venue and authors, then each identifier, linked.
    cite = " ".join(trial.locator(".cite").inner_text().split())
    assert cite.startswith("R") and "2021 Journal of Student Mental Health Kim J, Lee S, Park H, et al." in cite, cite
    ids = [" ".join(x.split()) for x in trial.locator(".ids .id").all_inner_texts()]
    assert ids == ["PMCID PMC9000001 free full text", "PMID 11111111", "DOI 10.1000/rct1", "NCT01234567"], ids
    assert trial.locator(".ids a").evaluate_all("links => links.map((a) => a.href)") == [
        "https://pmc.ncbi.nlm.nih.gov/articles/PMC9000001/", "https://pubmed.ncbi.nlm.nih.gov/11111111/",
        "https://doi.org/10.1000/rct1", "https://clinicaltrials.gov/study/NCT01234567"]
    # Excluding the whole paper sits beside the title.
    assert trial.locator(".paper-title button.exclude-paper").inner_text() == "Exclude the whole paper"
    # The authors open a table of every author and affiliation.
    trial.locator(".authors-btn").click()
    table = page.locator(".authors-pop:popover-open")
    table.locator("tbody tr").first.wait_for()
    assert table.locator("strong").inner_text() == "Authors (4)"
    rows = [[c.strip() for c in r.split("\t")] for r in table.locator("tbody tr").all_inner_texts()]
    assert rows[0] == ["1", "Jisoo Kim", "Department of Psychiatry, Seoul National University, Seoul, Korea."], rows
    # Each affiliation on its own line; footnote markers and email addresses are gone.
    assert rows[1][2] == "College of Nursing, Yonsei University, Seoul, Korea.\nMind Health Institute, Seoul, Korea." and rows[3][2] == "—"
    assert rows[2][2] == "Department of Statistics, Example University, Seoul, Korea\nBiostatistics Unit, Example Hospital, Seoul, Korea."
    page.keyboard.press("Escape")
    assert page.locator(".authors-pop:popover-open").count() == 0
    # Opened quotes stay open when the card is redrawn after a decision.
    open_paper(page, "A mindfulness app for depressive").locator(".result").first.locator("details.quotes summary").click()
    open_paper(page, "A mindfulness app for depressive").locator(".result").first.locator("button.include").click()
    page.wait_for_selector(".result[data-decision='include']")
    assert page.locator(".result").first.locator("details.quotes").get_attribute("open") is not None
    # Every result of this paper is decided: it counts as reviewed, and Next is the button to press.
    page.wait_for_function("document.querySelector('#review-progress-text').textContent === 'Reviewed 1 of 7 papers'")
    assert page.get_attribute("#queue button[aria-current='true']", "data-state") == "included"
    assert "ready" in page.get_attribute("#review-next", "class") and page.is_visible("#review-next-open")
    page.click(".exports-box summary")
    with page.expect_download() as download:
        page.click("#export-included")
    rows = list(csv.DictReader(io.StringIO(open(download.value.path(), encoding="utf-8").read())))
    assert len(rows) == 6 and {r["id"] for r in rows} == {"11111111"} and rows[0]["quote_check"] == "exact"

    # Everything is in this browser's storage: a reload brings the search and the decision back.
    # A record stored before authors were kept gets them from PubMed when its table opens.
    page.evaluate("""async () => {
        const { Store } = await import('/lib/store.js');
        const store = new Store();
        const paper = (await store.papers(location.hash.slice(1))).find((p) => p.ext_id === '22222222');
        await store.updatePaper(paper.id, { author_list: null });
    }""")
    page.reload()
    page.wait_for_selector("#review:not([hidden]) .paper")
    open_paper(page, "Brief mindfulness training").locator(".authors-btn").click()
    fetched = page.locator(".authors-pop:popover-open")
    fetched.locator("tbody tr").first.wait_for()
    assert [c.strip() for c in fetched.locator("tbody tr").first.inner_text().split("\t")] == ["1", "Mindful Students Trial Group", "—"]
    page.keyboard.press("Escape")
    assert page.evaluate("""async () => {
        const { Store } = await import('/lib/store.js');
        return (await new Store().papers(location.hash.slice(1))).find((p) => p.ext_id === '22222222').author_list[0].name;
    }""") == "Mindful Students Trial Group"
    assert open_paper(page, "A mindfulness app for depressive").locator(".result[data-decision='include']").count() == 1
    assert page.input_value("#api-key") == KEY
    assert page.evaluate("""async () => {
        const { Store } = await import('/lib/store.js');
        return (await new Store().project(location.hash.slice(1))).spec.statistics_needed[0].statistics;
    }""") == ["mean", "sd", "n", "mean_difference"]

    page.locator("#papers .paper").locator("button", has_text="Show source text").click()
    page.wait_for_selector("#source-text mark")
    page.click("#source-close")

    # The summary: for each effect size to find, the values side by side, described by Opus by key and
    # shown as printed. Nothing is pooled or recommended.
    page.click("#tab-summary")
    page.click("#summarize")
    page.wait_for_selector("#summary-body .summary-overview", timeout=15000)
    body = page.inner_text("#summary-body")
    assert "Pooled" not in body and "Recommended" not in body
    need = page.locator("#summary-body .summary-block.need").first
    assert need.locator("h3").inner_text() == "The difference and its SD"
    # The SDs as printed, smallest and largest, each with the record it came from.
    chips = need.locator("p .cand").all_inner_texts()
    assert [c.split()[0] for c in chips] == ["3.8", "4.5"] and all(c.split()[1].startswith("R") for c in chips)
    # One row per study arm, a column per statistic.
    arms = page.evaluate("""async () => {
        const { Store } = await import('/lib/store.js');
        const { summary } = await new Store().project(location.hash.slice(1));
        const values = new Map(summary.evidence.values.map((v) => [v.key, v]));
        return new Set(summary.result.needs[0].values.map((k) => values.get(k)).map((v) => JSON.stringify([v.record, v.outcome, v.timepoint, v.group]))).size;
    }""")
    assert need.locator("tbody tr").count() == arms
    assert need.locator("thead th").all_inner_texts() == ["Study", "Outcome", "Group", "n", "Mean", "SD", "Check"]
    # The registry record's values are listed, marked as the same participants as its paper.
    registry_rows = need.locator("tbody tr", has_text="same participants as")
    assert registry_rows.count() > 0 and all("Mindfulness App" in t for t in registry_rows.all_inner_texts())
    # Each study links to its page: a paper to PubMed or Europe PMC, a registry record to ClinicalTrials.gov.
    studies = need.locator("tbody tr td:first-child a")
    assert studies.count() == arms
    hrefs = studies.evaluate_all("links => links.map((a) => [a.textContent, a.href, a.target])")
    assert all(target == "_blank" for _, _, target in hrefs)
    assert all(href.startswith(("https://pmc.ncbi.nlm.nih.gov/articles/PMC", "https://pubmed.ncbi.nlm.nih.gov/", "https://europepmc.org/",
                                "https://clinicaltrials.gov/study/NCT"))
               for _, href, _ in hrefs), hrefs
    assert registry_rows.first.locator("a").get_attribute("href") == "https://clinicaltrials.gov/study/NCT01234567"
    # Records the text names by key are linked too, and so are those in the overlap list.
    named = page.locator("#summary-body .summary-overview a")
    assert named.count() == 1 and named.inner_text().startswith("R") and named.get_attribute("href").startswith("https://")
    assert page.locator("#summary-body .summary-block li a").count() >= 5
    assert "one study reported more than once" in body and "No dropout rates." in body
    assert page.locator("#summary-body details summary").inner_text().startswith("Every value kept (")
    # The agent panel shows the number with its key, not the bare key.
    assert "the largest SD is 4.5 [V" in page.inner_text("#activity")

    # The whole agent activity log exports, oldest first: a row per step with when it ran, the models
    # that served it, its tokens and its whole outcome; the JSON says which steps were part of which.
    total = page.locator("#timeline li").count()
    assert page.inner_text("#log-count") == f"Full log: {total} steps" and not page.is_visible("#timeline-all")
    with page.expect_download() as download:
        page.click("#export-log-csv")
    assert download.value.suggested_filename == f"agent-log-{page.url.split('#')[1]}.csv"
    log = list(csv.DictReader(io.StringIO(open(download.value.path(), encoding="utf-8").read())))
    assert len(log) == total and [r["at"] for r in log] == sorted(r["at"] for r in log)
    assert list(log[0]) == ["at", "started", "agent", "models", "step", "phase", "job", "parent", "paper_id", "paper_title",
                            "text", "detail", "ms", "calls", "input_tokens", "output_tokens"]
    summarized = next(r for r in log if r["step"] == "summarize")
    assert summarized["agent"] == "manager" and summarized["models"] == OPUS and int(summarized["input_tokens"]) > 0
    assert summarized["started"] <= summarized["at"] and summarized["phase"] == "done"
    read = next(r for r in log if r["step"] == "extract" and "Brief mindfulness" in r["text"])
    assert read["models"] == HAIKU and read["paper_title"].startswith("Brief mindfulness") and "PHQ-9" in read["detail"]
    with page.expect_download() as download:
        page.click("#export-log-json")
    exported = json.loads(open(download.value.path(), encoding="utf-8").read())
    events = exported["events"]
    assert exported["search"]["id"] == page.url.split("#")[1] and exported["agents"]["manager"] == "Opus 5.5"
    assert len(events) == total and "project_id" not in events[0]
    jobs = {e["job"] for e in events}
    searches = [e for e in events if e["parent"]]  # the searches Opus ran while planning
    assert searches and all(e["parent"] in jobs for e in searches)
    assert {next(x for x in events if x["job"] == e["parent"])["step"] for e in searches} == {"search"}
    # A decision made after the summary marks it out of date.
    page.click("#tab-review")
    open_paper(page, "Brief mindfulness training").locator(".result").first.locator("button.exclude").click()
    page.click("#tab-summary")
    page.wait_for_selector("#summary-body .stale")
    page.click("#tab-review")

    # Requests to Anthropic: each model with its settings, straight from the page with the user's key.
    calls = network.claude
    assert calls and all(h.get("anthropic-dangerous-direct-browser-access") == "true" for h, _ in calls)
    assert all(KEY in (h.get("x-api-key", "") + h.get("authorization", "")) for h, _ in calls)
    steps = {}
    for headers, body in calls:
        system = (body.get("system") or [{"text": ""}])[0]["text"]
        step = next((s for s in ("SPEC", "SEARCH", "SCREEN", "EXTRACT", "ADJUDICATE", "OVERLAP", "SUMMARIZE")
                     if system.startswith(getattr(prompts, s))), "FETCH")
        steps.setdefault(step, (headers, body))
    assert set(steps) == {"SPEC", "SEARCH", "SCREEN", "EXTRACT", "ADJUDICATE", "OVERLAP", "SUMMARIZE", "FETCH"}
    expected = {"SPEC": (OPUS, "high"), "SEARCH": (OPUS, "high"), "ADJUDICATE": (OPUS, "xhigh"), "OVERLAP": (OPUS, "high"), "SUMMARIZE": (OPUS, "high"),
                "FETCH": (SONNET, "low"), "SCREEN": (HAIKU, "low"), "EXTRACT": (HAIKU, "medium")}
    for step, (model, effort) in expected.items():
        headers, body = steps[step]
        assert (body["model"], body["output_config"]["effort"]) == (model, effort), step
        if model == HAIKU:
            assert "fallbacks" not in body, step
        else:
            assert body["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in headers.get("anthropic-beta", ""), step
    assert '"description": "Undergraduate students"' in steps["SEARCH"][1]["messages"][0]["content"]  # the edit was searched
    fetch = steps["FETCH"][1]
    assert fetch["tools"] == [{"type": "web_fetch_20250910", "name": "web_fetch", "max_uses": 1}]
    assert PDF_URL in fetch["messages"][0]["content"]

    # A run cut short (the tab closed) is picked up again with Continue.
    project_id = page.evaluate("location.hash.slice(1)")
    page.evaluate("""async (id) => {
        const { Store } = await import('/lib/store.js');
        const store = new Store();
        await store.updateProject(id, { status: 'extracting' });
        const pilot = (await store.papers(id)).find((p) => p.ext_id === '44444444');
        await store.updatePaper(pilot.id, { status: 'extracting', extraction: null });
    }""", project_id)
    page.reload()
    page.wait_for_selector("#continue:not([hidden])")
    assert "before it finished" in page.inner_text("#project-list")
    page.click("#continue")
    page.wait_for_selector("#tab-review[aria-selected='true']", timeout=30000)
    page.wait_for_function("document.querySelector('#tabs button').textContent === 'Extracted (7)'")
    assert "Continuing the search" in page.inner_text("#timeline")

    phone = context.new_page()
    phone.set_viewport_size({"width": 390, "height": 844})
    phone.goto(page.url)
    phone.wait_for_selector("#review:not([hidden]) .paper")
    assert phone.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth") == 0
    # On a narrow screen the activity panel covers the page, so it waits to be asked for.
    assert not phone.is_visible("#activity")
    if os.environ.get("ESF_SCREENSHOTS"):
        page.screenshot(path=os.path.join(os.environ["ESF_SCREENSHOTS"], "site-review.png"), full_page=True)
        phone.screenshot(path=os.path.join(os.environ["ESF_SCREENSHOTS"], "site-phone.png"), full_page=True)
    phone.click("#activity-toggle")
    assert phone.is_visible("#activity")
    if os.environ.get("ESF_SCREENSHOTS"):
        phone.screenshot(path=os.path.join(os.environ["ESF_SCREENSHOTS"], "site-phone-activity.png"))
    phone.keyboard.press("Escape")
    assert not phone.is_visible("#activity")
    context.close()
    assert errors == []


def test_the_page_says_what_is_wrong_with_a_key(browser, site_url):
    context = browser.new_context()
    context.route("**/*", Network())
    page = context.new_page()
    page.goto(site_url)
    assert page.is_visible("#agents-page")

    # An NCBI key pasted into the Anthropic box is caught before anything is sent.
    page.fill("#api-key", "0123456789abcdef0123456789abcdef1234")
    assert "This looks like an NCBI API key, not an Anthropic API key" in page.inner_text("#key-status")
    page.click("#agents-done")
    page.click("#tab-problem")
    page.fill("#paragraph", "A randomized trial of a mindfulness app versus waitlist for PHQ-9 in students.")
    page.click("#read")  # not sent: the Agents page opens again and says why
    assert page.is_visible("#agents-page") and "It is not sent to Anthropic" in page.inner_text("#key-status")
    # A key of an unfamiliar shape only gets a warning.
    page.fill("#api-key", "a-new-kind-of-key")
    assert "doesn't look like an Anthropic API key: those start with sk-ant-" in page.inner_text("#key-status")
    assert page.get_attribute("#agents-toggle", "data-missing") == "false"

    # A refused key: Check key and a failed step both quote Anthropic's own error.
    page.fill("#api-key", "sk-ant-api03-not-a-real-key")
    page.click("#check-key")
    page.wait_for_function("document.querySelector('#key-status').textContent.includes('did not accept')")
    status = page.inner_text("#key-status")
    assert "claude-opus-5-5: Anthropic did not accept the API key (401: invalid x-api-key (request req_test))" in status
    page.click("#agents-done")
    page.click("#tab-problem")
    page.fill("#paragraph", "A randomized trial of a mindfulness app versus waitlist for PHQ-9 in students.")
    page.click("#read")
    page.wait_for_selector("#problem-error:not([hidden])")
    assert "Anthropic did not accept the API key (401: invalid x-api-key" in page.inner_text("#problem-error")

    # A good key: every model is reachable.
    page.click("#agents-toggle")
    page.fill("#api-key", KEY)
    page.click("#check-key")
    page.wait_for_function("document.querySelector('#key-status').textContent.startsWith('The key works')")
    assert page.inner_text("#key-status") == "The key works, and it may use Claude Opus 5.5, Claude Sonnet 5.5, Claude Haiku 5.5."
    context.close()


def test_a_key_pasted_under_the_other_provider_moves_with_the_person(browser, site_url):
    context = browser.new_context()
    context.route("**/*", Network())
    page = context.new_page()
    page.goto(site_url)
    assert page.is_visible("#agents-page")
    page.check("input[name='provider'][value='openai']")
    page.fill("#api-key", KEY)
    assert "choose Claude API to use it" in page.inner_text("#key-status")
    kept = "() => [sessionStorage.getItem('esf-openai-api-key'), sessionStorage.getItem('esf-anthropic-api-key')]"
    assert page.evaluate(kept) == [None, None]  # never kept as an OpenAI key
    # Following the advice carries it over.
    page.check("input[name='provider'][value='anthropic']")
    assert page.input_value("#api-key") == KEY and page.evaluate(kept) == [None, KEY]
    assert page.get_attribute("#agents-toggle", "data-missing") == "false"
    page.check("input[name='provider'][value='openai']")
    assert page.input_value("#api-key") == ""
    context.close()


def test_the_page_runs_on_openai_models(browser, site_url):
    """With OpenAI chosen in Setup, every step runs on OpenAI's Responses API with the user's key, the
    page names the OpenAI models, and nothing is fetched through a model (OpenAI has no web fetch)."""
    network = Network()
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    context.route("**/*", network)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(site_url)
    page.fill("#api-key", KEY)  # a Claude key first: switching keeps each provider's key apart
    page.check("input[name='provider'][value='openai']")
    assert page.inner_text("#api-key-label") == "OpenAI API key" and page.input_value("#api-key") == ""
    assert page.is_visible("#provider-note") and "the abstract is read instead" in page.inner_text("#provider-note")
    assert page.inner_text("#agents-label") == "OpenAI API" and page.get_attribute("#agents-toggle", "data-missing") == "true"
    page.fill("#api-key", "sk-ant-api03-wrong-provider")
    assert "This is an Anthropic API key, not an OpenAI API key: choose Claude API to use it" in page.inner_text("#key-status")
    page.fill("#api-key", "sk-proj-not-the-right-key-000")
    page.click("#check-key")
    page.wait_for_function("document.querySelector('#key-status').textContent.includes('did not accept')")
    assert "gpt-6.1-sol: OpenAI did not accept the API key" in page.inner_text("#key-status")
    page.fill("#api-key", OPENAI_KEY)
    page.click("#check-key")
    page.wait_for_function("document.querySelector('#key-status').textContent.startsWith('The key works')")
    assert page.inner_text("#key-status") == "The key works, and it may use GPT-6.1 Sol, GPT-6 Luna."
    assert page.locator("#roles li strong").all_inner_texts() == ["GPT-6.1 Sol · manager", "GPT-6 Luna · workers", "Code · search and checks"]
    assert page.inner_text("#models") == "GPT-6.1 Sol plans and reviews · GPT-6 Luna screens and reads"
    assert "sent to OpenAI's API" in page.inner_text("#agents-page .notice")
    assert page.locator("#model-worker option").all_inner_texts() == ["GPT-6.1 Sol", "GPT-6 Luna"]
    page.check("input[name='provider'][value='anthropic']")
    assert page.input_value("#api-key") == KEY and page.inner_text("#models").startswith("Opus 5.5 plans")
    # Each provider keeps its own choice of models.
    page.focus("#model-worker")
    page.select_option("#model-worker", "claude-sonnet-5-5")
    assert page.inner_text("#models") == "Opus 5.5 plans and reviews · Sonnet 5.5 fetches · Sonnet 5.5 screens and reads"
    assert page.evaluate("document.activeElement.id") == "model-worker"  # the redrawn list keeps the focus
    assert "Opus 5.5 · Sonnet 5.5 · Sonnet 5.5" in page.inner_text(".provider-card[data-provider='anthropic']")
    page.check("input[name='provider'][value='openai']")
    assert page.input_value("#api-key") == OPENAI_KEY and page.input_value("#model-worker") == "gpt-6-luna"
    page.click("#agents-done")
    assert page.is_hidden("#agents-page") and page.is_visible(".layout")
    assert page.evaluate("document.activeElement.id") == "agents-toggle"

    page.fill("#contact-email", "test@example.com")
    page.dispatch_event("#contact-email", "change")
    # A model key pasted in the NCBI box is neither kept nor sent to NCBI.
    page.fill("#ncbi-key", KEY)
    page.dispatch_event("#ncbi-key", "change")
    assert page.inner_text("#ncbi-status").startswith("This is an Anthropic API key, not an NCBI API key: it is not sent to NCBI")
    assert page.evaluate("localStorage.getItem('esf-ncbi-api-key')") is None
    page.click("#setup-done")
    page.fill("#paragraph", "We propose a two-arm randomized trial of a 12-week mindfulness app versus waitlist for PHQ-9 in students.")
    page.click("#read")
    page.wait_for_selector("#spec:not([hidden])")
    assert "How GPT-6.1 Sol read your paragraph" in page.inner_text("#spec .summary")
    page.click("#run")
    page.wait_for_selector("#tab-review[aria-selected='true']", timeout=30000)
    page.wait_for_selector("#review:not([hidden]) .paper")

    # The PDF the page may not download was not fetched through a model: that paper was read from its abstract.
    assert network.refused == [PDF_URL] and not network.claude
    pdf_paper = open_paper(page, "Brief mindfulness training")
    assert pdf_paper.locator(".badge", has_text="Fetched by").count() == 0
    assert pdf_paper.locator(".paper-details .badge", has_text="Abstract only").count() == 1
    pdf_paper.locator(".paper-details summary").click()
    assert "The open-access PDF could not be downloaded (example.org doesn't accept requests from web pages)" in pdf_paper.inner_text()
    assert open_paper(page, "Mindfulness groups and depressive").locator(".badge", has_text="Reviewed by GPT-6.1 Sol").count() == 1
    app = open_paper(page, "A mindfulness app for depressive")
    audited = "random audit" in app.text_content()  # one clean extraction in ten goes to the manager anyway
    assert app.locator(".badge", has_text="Reviewed by GPT-6.1 Sol" if audited else "Extracted by GPT-6 Luna").count() == 1
    names = [" ".join(x.split()) for x in page.locator("#agents .agent h3").all_inner_texts()]
    assert names == ["GPT-6.1 Sol manager", "GPT-6 Luna workers", "Code search and checks"], names
    assert "GPT-6.1 Sol" in page.locator("#timeline li .who").all_inner_texts()
    assert [x.inner_text() for x in page.locator("#map-body [data-stage='extract'] .map-agent").all()] == ["Code", "GPT-6 Luna", "GPT-6.1 Sol"]

    page.click("#tab-summary")
    assert page.inner_text("#summary .summary").startswith("GPT-6.1 Sol describes what the papers you kept report")
    page.click("#summarize")
    page.wait_for_selector("#summary-body .summary-overview", timeout=15000)

    calls = network.openai_calls
    assert calls and all(h["authorization"] == f"Bearer {OPENAI_KEY}" for h, _ in calls)
    pubmed = [str(r.url) for r in network.literature.requests if r.url.host == "eutils.ncbi.nlm.nih.gov"]
    assert pubmed and not any("api_key" in url or KEY in url for url in pubmed)
    steps = {}
    for _, body in calls:
        assert body["store"] is False and body["stream"] is True and "max_output_tokens" not in body and "temperature" not in body
        assert all(item.get("role") != "system" for item in body["input"])
        step = next(s for s in ("SPEC", "SEARCH", "SCREEN", "EXTRACT", "ADJUDICATE", "OVERLAP", "SUMMARIZE")
                    if body["instructions"].startswith(getattr(prompts, s)))
        steps.setdefault(step, body)
    expected = {"SPEC": ("gpt-6.1-sol", "high"), "SEARCH": ("gpt-6.1-sol", "high"), "ADJUDICATE": ("gpt-6.1-sol", "xhigh"),
                "OVERLAP": ("gpt-6.1-sol", "high"), "SUMMARIZE": ("gpt-6.1-sol", "high"), "SCREEN": ("gpt-6-luna", "low"),
                "EXTRACT": ("gpt-6-luna", "medium")}
    assert set(steps) == set(expected)
    for step, (model, effort) in expected.items():
        assert (steps[step]["model"], steps[step]["reasoning"]["effort"]) == (model, effort), step
        if step != "SEARCH":
            assert steps[step]["text"]["format"]["strict"] is True and steps[step]["text"]["format"]["type"] == "json_schema"
    search = steps["SEARCH"]
    assert [t["name"] for t in search["tools"]] == ["count_pubmed", "search_pubmed", "search_europepmc", "search_clinicaltrials", "submit_search_plan"]
    assert all(t["type"] == "function" and t["strict"] for t in search["tools"])
    assert search["include"] == ["reasoning.encrypted_content"]
    # The searches the model asked for ran in the page, a null max_results taking the default.
    assert network.openai.tool_outputs["c1"] == f"#1 4 | mindfulness[tiab]\n#2 1 | mindfulness[tiab] AND {REVIEW_FILTER}"
    assert "| PPR | preprint |" in network.openai.tool_outputs["c2"]
    assert not errors, errors


def test_a_search_saved_before_openai_keeps_its_credits(browser, site_url):
    """A search run with Claude before the agents were named by role still credits Claude's models,
    whichever provider is chosen now; a key is never sent to the other vendor."""
    network = Network()
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    context.route("**/*", network)
    page = context.new_page()
    page.goto(site_url)
    papers = [
        _paper(1, [("PHQ-9", "12 weeks", [("App", "mean", 8.1), ("App", "sd", 4.2)])], ext_id="1001", extracted_by="manager", fetched_by="sonnet",
               fulltext_kind="oa_pdf", escalation={"reasons": ["a quote is not in the source"], "audit": False}),
        _paper(2, [("PHQ-9", "12 weeks", [("App", "mean", 9.0)])], ext_id="1002", extracted_by="worker"),
    ]
    project_id = page.evaluate("""async ([papers, spec]) => {
        const { Store } = await import('/lib/store.js');
        const store = new Store();
        const id = await store.createProject('A randomized trial of a mindfulness app versus waitlist for PHQ-9 in students.');
        await store.updateProject(id, { status: 'done', spec, progress: { stage: 'done', done: 0, total: 0 },
          search_plan: { levels: [{ level: 'primary', query: 'q', why: '' }], clinicaltrials_queries: [], extra_ids: [] } });
        for (const { id: _, ...paper } of papers) await store.addPaper(id, paper);
        const usage = { input_tokens: 10, output_tokens: 5 };
        await store.addUsage(id, 'spec', 'claude-opus-5-5', usage);
        await store.addUsage(id, 'search', 'claude-opus-5-5', usage);
        await store.addUsage(id, 'fetch', 'claude-sonnet-5-5', usage);
        await store.addUsage(id, 'extract', 'claude-haiku-5-5', usage);
        await store.addEvent(id, { agent: 'opus', step: 'search', phase: 'done', text: 'Planning the search', models: ['claude-opus-5-5'] });
        await store.addEvent(id, { agent: 'sonnet', step: 'fetch', phase: 'done', text: 'Fetching a PDF', models: ['claude-sonnet-5-5'] });
        await store.addEvent(id, { agent: 'haiku', step: 'extract', phase: 'done', text: 'Extracting a trial', models: ['claude-haiku-5-5'] });
        return id;
    }""", [papers, SPEC.model_dump()])
    page.evaluate("""() => { localStorage.setItem('esf-provider', 'openai'); localStorage.setItem('esf-openai-api-key', 'sk-proj-test-site-0123456789'); }""")
    page.goto(f"{site_url}#{project_id}")
    page.reload()
    page.wait_for_selector("#review:not([hidden]) .paper")
    assert page.inner_text("#agents-label") == "OpenAI API"
    # The papers, the agents' cards, the timeline and the map credit Claude's models, which did the work.
    reviewed = open_paper(page, "Trial 1")
    reviewed.locator(".paper-details summary").click()
    assert reviewed.locator(".badge", has_text="Reviewed by Opus 5.5").count() == 1
    assert reviewed.locator(".badge", has_text="Fetched by Sonnet 5.5").count() == 1
    assert "Why Opus 5.5 reviewed it: a quote is not in the source" in reviewed.inner_text()
    extracted = open_paper(page, "Trial 2")
    extracted.locator(".paper-details summary").click()
    assert extracted.locator(".badge", has_text="Extracted by Haiku 5.5").count() == 1
    # The review's arrow keys do nothing while the Agents page covers it; Escape closes it and gives the focus back.
    page.click("#agents-toggle")
    page.evaluate("document.activeElement.blur()")
    page.keyboard.press("ArrowLeft")
    page.keyboard.press("Escape")
    assert page.is_hidden("#agents-page") and page.evaluate("document.activeElement.id") == "agents-toggle"
    assert "Trial 2" in page.inner_text("#papers .paper h3")
    names = [" ".join(x.split()) for x in page.locator("#agents .agent h3").all_inner_texts()]
    assert names == ["Opus 5.5 manager", "Sonnet 5.5 fetcher", "Haiku 5.5 workers", "Code search and checks"], names
    assert page.locator("#timeline li .who").all_inner_texts() == ["Haiku 5.5", "Sonnet 5.5", "Opus 5.5"]
    page.click("#activity-filter button:has-text('Sonnet')")
    assert page.locator("#timeline li .who").all_inner_texts() == ["Sonnet 5.5"]
    page.click("#tab-progress")
    chips = page.locator("#map-body [data-stage='extract'] .map-agent").all_inner_texts()
    assert chips == ["Code", "Sonnet 5.5", "Haiku 5.5", "Opus 5.5"], chips
    assert "downloaded by Sonnet 5.5" in page.inner_text("#map-body [data-stage='extract']")
    page.click("#tab-spec")
    assert page.inner_text("#spec .summary").startswith("How Opus 5.5 read your paragraph")
    page.click("#tab-summary")
    assert page.inner_text("#summary .summary").startswith("GPT-6.1 Sol describes")  # not written yet: the chosen manager will

    # After Search again (here, one started after every call above), the earlier search's models are not
    # credited; reading the problem still is.
    page.evaluate("""async (id) => {
        const { Store } = await import('/lib/store.js');
        await new Store().updateProject(id, { run_started_at: '2100-01-01T00:00:00Z' });
    }""", project_id)
    page.reload()
    page.wait_for_selector("#review:not([hidden]) .paper")
    names = [" ".join(x.split()) for x in page.locator("#agents .agent h3").all_inner_texts()]
    assert names == ["Opus 5.5 manager", "GPT-6 Luna workers", "Code search and checks"], names

    # A dated snapshot is named as its model; a host that failed once is tried again without a fetcher.
    labels = page.evaluate("""async () => {
        const { modelLabel } = await import('/lib/providers.js');
        return [modelLabel('gpt-6.1-sol-2026-08-12'), modelLabel('claude-haiku-5-5')];
    }""")
    assert labels == ["GPT-6.1 Sol", "Haiku 5.5"]
    retried = page.evaluate("""async () => {
        const { Http } = await import('/lib/sources.js');
        let calls = 0;
        const http = new Http({ fetchImpl: async () => {
            calls += 1;
            if (calls === 1) throw new TypeError('Failed to fetch');
            return new Response('{"ok": true}', { headers: { 'content-type': 'application/json' } });
        } });
        let first;
        try { await http.get('https://eutils.ncbi.nlm.nih.gov/x'); } catch (error) { first = error.message; }
        return [first, await http.get('https://eutils.ncbi.nlm.nih.gov/x')];
    }""")
    assert retried == ["eutils.ncbi.nlm.nih.gov doesn't accept requests from web pages.", {"ok": True}]

    # An Anthropic key in the OpenAI box is caught, and nothing goes to OpenAI.
    page.click("#agents-toggle")
    page.fill("#api-key", KEY)
    page.click("#agents-done")
    page.click("#new-project")
    page.fill("#paragraph", "A randomized trial of a mindfulness app versus waitlist for PHQ-9 in students.")
    page.click("#read")
    assert page.is_visible("#agents-page") and "This is an Anthropic API key, not an OpenAI API key" in page.inner_text("#key-status")
    assert not network.openai_calls and not network.claude
    context.close()


def test_a_run_stops_when_the_openai_account_runs_out_of_credit(browser, site_url):
    network = Network()
    network.openai_quota = 2
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    context.route("**/*", network)
    page = context.new_page()
    page.goto(site_url)
    page.check("input[name='provider'][value='openai']")
    page.fill("#api-key", OPENAI_KEY)
    page.click("#agents-done")
    page.fill("#contact-email", "test@example.com")
    page.dispatch_event("#contact-email", "change")
    page.click("#setup-done")
    page.fill("#paragraph", "We propose a two-arm randomized trial of a 12-week mindfulness app versus waitlist for PHQ-9 in students.")
    page.click("#read")
    page.wait_for_selector("#spec:not([hidden])")
    page.click("#run")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and page.evaluate("""async () => {
        const { Store } = await import('/lib/store.js');
        return (await new Store().project(location.hash.slice(1))).status;
    }""") != "error":
        time.sleep(0.2)
    project = page.evaluate("""async () => {
        const { Store } = await import('/lib/store.js');
        const store = new Store();
        const id = location.hash.slice(1);
        return { project: await store.project(id), statuses: (await store.papers(id)).map((p) => p.status) };
    }""")
    assert "run out of credit" in (project["project"]["error"] or ""), (project["project"]["status"], project["project"]["error"], project["statuses"])
    screened = [b for _, b in network.openai_calls if b.get("text", {}).get("format", {}).get("name") == "ScreenDecision"]
    assert len(screened) <= 2 + 6  # the refused calls already under way, then no more
    assert "unsure" not in project["statuses"] and "candidate" in project["statuses"]
    context.close()
