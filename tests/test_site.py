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
from dataclasses import asdict
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import httpx
import pytest

from esf import prompts
from esf.config import REPO_ROOT
from esf.jats import jats_to_text
from esf.sources import parse_pubmed_xml, trial_results_text
from esf.verify import in_audit_sample, numbers_in, verify
from fixtures import (
    ABSTRACT_LINE, JATS, PDF_LINE, PUBMED_XML, SPEC, TRIAL, FakeLLM, LiteratureAPIs, _extraction, _result, _value, make_pdf,
    streamed,
)

sync_api = pytest.importorskip("playwright.sync_api")
SITE = REPO_ROOT / "site"
if not (SITE / "vendor" / "anthropic-sdk.mjs").exists():
    pytest.skip("build the site first: cd site && npm ci && npm run build", allow_module_level=True)

KEY = "sk-ant-test-site"
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

    def __init__(self, refuse_web_pages=("example.org",)):
        self.literature = LiteratureAPIs()
        self.refuse = set(refuse_web_pages)
        self.llm = FakeLLM()
        self.claude: list[tuple[dict, dict]] = []
        self.refused: list[str] = []

    def __call__(self, route, request):
        host = urlparse(request.url).hostname
        if host in ("127.0.0.1", "localhost"):
            return route.continue_()
        if request.method == "OPTIONS":
            return route.fulfill(status=204, headers=PREFLIGHT)
        if host == "api.anthropic.com":
            return self.anthropic(route, request)
        if host in self.refuse:
            self.refused.append(request.url)
            return route.abort("failed")
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

    def structured(self, body):
        system = body["system"][0]["text"]
        content = body["messages"][0]["content"]
        if system.startswith(prompts.SPEC):
            return SPEC.model_dump()
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
                {"type": "tool_use", "id": "t1", "name": "search_pubmed", "input": {"query": "mindfulness[tiab] AND depress*[tiab]", "max_results": 5}},
                {"type": "tool_use", "id": "t2", "name": "search_clinicaltrials", "input": {"query": "mindfulness AND depression"}},
            ], "tool_use")
        if turns == 3:
            results = json.dumps(body["messages"][2]["content"])
            assert "Total hits: 4" in results and "NCT01234567" in results
            plan = {"pubmed_queries": ["mindfulness[tiab] AND depress*[tiab]"], "clinicaltrials_queries": ["mindfulness AND depression"],
                    "extra_ids": [], "summary": "One query in each source."}
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
        "verify": [[e.model_dump(), t, r] for e, t, r in cases],
    }
    result = page.evaluate("""async (data) => {
        const jats = await import('/lib/jats.js');
        const sources = await import('/lib/sources.js');
        const v = await import('/lib/verify.js');
        return {
            jats: jats.jatsToText(data.jats),
            trial: sources.trialResultsText(data.trial),
            pubmed: sources.parsePubmedXml(data.pubmed),
            numbers: data.numbers.map(v.numbersIn),
            verify: data.verify.map(([e, t, r]) => v.verify(e, t, r)),
            audit: await Promise.all(data.keys.map((k) => v.inAuditSample(k, 0.1))),
            g: data.g.map(v.g),
        };
    }""", data)
    assert result["jats"] == jats_to_text(JATS)
    assert result["trial"] == trial_results_text(TRIAL)
    assert result["pubmed"] == [asdict(r) for r in parse_pubmed_xml(PUBMED_XML)]
    assert result["numbers"] == [numbers_in(s) for s in numbers]
    assert result["verify"] == [verify(e, t, r).to_dict() for e, t, r in cases]
    assert result["audit"] == [in_audit_sample(k, 0.1) for k in keys]
    assert result["g"] == [format(x, "g") for x in g_cases]


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
    network = Network()
    context = browser.new_context(viewport={"width": 1280, "height": 900}, accept_downloads=True)
    context.route("**/*", network)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    # The refused PDF request is expected to log a failed load; any other error is a bug.
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" and not m.location.get("url", "").startswith("https://example.org/") else None)

    page.goto(site_url)
    page.fill("#paragraph", "We propose a two-arm randomized trial of a 12-week mindfulness app versus waitlist for PHQ-9 in students.")
    page.click("#read")
    assert page.get_attribute(".keybox", "data-missing") == "true"
    page.fill("#api-key", KEY)
    page.fill("#contact-email", "test@example.com")
    page.dispatch_event("#contact-email", "change")
    page.click("#read")
    page.wait_for_selector("#spec:not([hidden])")
    assert page.input_value("#f-population") == "University students"
    page.click("#run")
    # The page shows the search while it runs and moves on to the review when it finishes (it only
    # does that when the search tab is the one open, so this also shows the search tab was followed).
    page.wait_for_selector("#tab-review[aria-selected='true']", timeout=30000)
    page.wait_for_selector("#review:not([hidden]) .paper")
    assert page.is_hidden("#progress-card") and page.is_hidden("#spec") and page.is_hidden("#problem")

    # Papers and registry records are reviewed separately.
    sources = page.locator("#source-switch button").all_inner_texts()
    assert sources == ["Papers (PubMed) · 3 extracted of 4", "ClinicalTrials.gov · 1 extracted of 1"], sources
    tabs = page.locator("#tabs button").all_inner_texts()
    assert tabs[0] == "Extracted (3)" and tabs[3] == "Screened out (1)", tabs
    page.click("#source-switch button:has-text('ClinicalTrials.gov')")
    assert page.locator("#papers .paper").all_inner_texts()[0].startswith("A Randomized Trial of a Mindfulness App")
    page.click("#source-switch button:has-text('Papers')")
    page.click("#tab-progress")
    counts = page.locator("#source-counts div").all_inner_texts()
    assert counts == ["Papers (PubMed)\n4 found · 3 kept after screening · 3 extracted",
                      "ClinicalTrials.gov\n1 found · 1 kept after screening · 1 extracted"], counts
    page.click("#tab-review")

    # The activity panel: what each agent did, newest first.
    assert page.is_visible("#activity")
    agents = page.locator("#agents .agent").all_inner_texts()
    names = [" ".join(x.split()) for x in page.locator("#agents .agent h3").all_inner_texts()]
    assert names == ["Opus 5.5 manager", "Sonnet 5.5 fetcher", "Haiku 5.5 workers", "Code search and checks"], names
    assert "calls" in agents[0] and "Idle" in agents[0]
    timeline = "\n".join(page.locator("#timeline li").all_inner_texts())
    for expected in ("Reading the problem", "Tries PubMed: mindfulness", "Planning the search", "Collected 5 records to screen",
                     "Screening “Stress responses in a mouse model", "exclude: Animal study.",
                     "Fetching example.org, which the browser may not read", "PDF, 1 KB",
                     "Extracting “Brief mindfulness training", "Reviewing “Mindfulness groups", "Search finished"):
        assert expected in timeline, expected
    page.click("#activity-filter button:has-text('Sonnet')")
    assert page.locator("#timeline li .who").all_inner_texts() == ["Sonnet 5.5"]
    page.click("#activity-filter button:has-text('All')")

    assert network.refused == [PDF_URL]  # tried once directly, then fetched through Claude
    pdf_paper = page.locator(".paper", has_text="Brief mindfulness training")
    assert pdf_paper.locator(".badge", has_text="Fetched by Sonnet").count() == 1
    assert pdf_paper.locator(".badge", has_text="Open-access PDF").count() == 1
    assert pdf_paper.locator(".badge", has_text="quote found").count() == 4
    assert page.locator(".paper", has_text="pilot randomized trial").locator(".badge", has_text="Reviewed by Opus").count() == 1

    page.locator(".result").first.locator("button.include").click()
    page.wait_for_selector(".result[data-decision='include']")
    with page.expect_download() as download:
        page.click("#export-included")
    rows = list(csv.DictReader(io.StringIO(open(download.value.path(), encoding="utf-8").read())))
    assert len(rows) == 6 and {r["id"] for r in rows} == {"11111111"} and rows[0]["quote_check"] == "exact"

    # Everything is in this browser's storage: a reload brings the search and the decision back.
    page.reload()
    page.wait_for_selector("#review:not([hidden]) .paper")
    assert page.locator(".result[data-decision='include']").count() == 1
    assert page.input_value("#api-key") == KEY

    page.locator(".paper", has_text="A mindfulness app for depressive").locator("button", has_text="Show source text").click()
    page.wait_for_selector("#source-text mark")
    page.click("#source-close")

    # Requests to Anthropic: each model with its settings, straight from the page with the user's key.
    calls = network.claude
    assert calls and all(h.get("anthropic-dangerous-direct-browser-access") == "true" for h, _ in calls)
    assert all(KEY in (h.get("x-api-key", "") + h.get("authorization", "")) for h, _ in calls)
    steps = {}
    for headers, body in calls:
        system = (body.get("system") or [{"text": ""}])[0]["text"]
        step = next((s for s in ("SPEC", "SEARCH", "SCREEN", "EXTRACT", "ADJUDICATE") if system.startswith(getattr(prompts, s))), "FETCH")
        steps.setdefault(step, (headers, body))
    assert set(steps) == {"SPEC", "SEARCH", "SCREEN", "EXTRACT", "ADJUDICATE", "FETCH"}
    expected = {"SPEC": (OPUS, "high"), "SEARCH": (OPUS, "high"), "ADJUDICATE": (OPUS, "xhigh"),
                "FETCH": (SONNET, "low"), "SCREEN": (HAIKU, "low"), "EXTRACT": (HAIKU, "medium")}
    for step, (model, effort) in expected.items():
        headers, body = steps[step]
        assert (body["model"], body["output_config"]["effort"]) == (model, effort), step
        if model == HAIKU:
            assert "fallbacks" not in body, step
        else:
            assert body["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in headers.get("anthropic-beta", ""), step
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
    page.wait_for_function("document.querySelector('#tabs button').textContent === 'Extracted (3)'")
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

    # An NCBI key pasted into the Anthropic box is caught before anything is sent.
    page.fill("#api-key", "0123456789abcdef0123456789abcdef1234")
    assert "doesn't look like an Anthropic API key" in page.inner_text("#key-status")

    # A refused key: Check key and a failed step both quote Anthropic's own error.
    page.fill("#api-key", "sk-ant-api03-not-a-real-key")
    page.click("#check-key")
    page.wait_for_function("document.querySelector('#key-status').textContent.includes('did not accept')")
    status = page.inner_text("#key-status")
    assert "claude-opus-5-5: Anthropic did not accept the API key (401: invalid x-api-key (request req_test))" in status
    page.fill("#paragraph", "A randomized trial of a mindfulness app versus waitlist for PHQ-9 in students.")
    page.click("#read")
    page.wait_for_selector("#problem-error:not([hidden])")
    assert "Anthropic did not accept the API key (401: invalid x-api-key" in page.inner_text("#problem-error")

    # A good key: every model is reachable.
    page.fill("#api-key", KEY)
    page.click("#check-key")
    page.wait_for_function("document.querySelector('#key-status').textContent.startsWith('The key works')")
    assert page.inner_text("#key-status") == "The key works, and it may use Claude Opus 5.5, Claude Sonnet 5.5, Claude Haiku 5.5."
    context.close()
