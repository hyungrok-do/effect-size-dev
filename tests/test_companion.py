"""The hosted page's subscription bridge: real cross-origin windows, local HTTP and fake OpenAI."""

import json
from urllib.parse import urlparse

import httpx
from fastapi.testclient import TestClient

from esf.config import Settings
from esf.openai_llm import OpenAILLM
from fixtures import FakeOpenAI
from test_chatgpt import signed_in
from test_server import BASE, make_client
from test_site import Network, SITE, _paper, browser, site_url, stored_search
from test_web import ACCESS_TOKEN, PARAGRAPH, serving


BODY = {"model": "gpt-6.1-sol", "instructions": "Read a study plan.",
        "input": [{"role": "user", "content": [{"type": "input_text", "text": PARAGRAPH}]}],
        "reasoning": {"effort": "xhigh"}, "text": {"format": {"name": "SearchSpec"}}, "store": False, "stream": True}


def bridge_client(tmp_path, responses=None):
    session, _, _ = signed_in(tmp_path)
    responses = responses or FakeOpenAI()

    def factory(chatgpt):
        return OpenAILLM(Settings(data_dir=tmp_path), chatgpt.access_token,
                         client=httpx.Client(transport=httpx.MockTransport(responses)),
                         reasoning_levels=chatgpt.reasoning_levels, on_error=chatgpt.handle_api_error)

    client = make_client(tmp_path, chatgpt=session, llm_for_chatgpt=factory, access_token=ACCESS_TOKEN)
    client.headers["X-ESF-Token"] = ACCESS_TOKEN
    return client, session, responses


def test_companion_responses_keep_credentials_local_and_apply_plan_constraints(tmp_path):
    client, session, responses = bridge_client(tmp_path)
    result = client.post("/api/chatgpt/responses", json=BODY)
    assert result.status_code == 200 and result.json()["status"] == "completed"
    assert result.headers["cache-control"] == "no-store"
    request, body = responses.requests[0]
    assert request.headers["authorization"] == "Bearer at-1"
    assert body["reasoning"] == {"effort": "high"}  # this plan does not offer xhigh
    assert body["store"] is False and body["stream"] is True
    assert "at-1" not in result.text and "rt-1" not in result.text and ACCESS_TOKEN not in result.text
    assert "access-control-allow-origin" not in result.headers
    assert client.post("/api/chatgpt/responses", json={**BODY, "model": "internal-eval"}).status_code == 403
    assert client.post("/api/chatgpt/responses", json={**BODY, "tools": [{"type": "function"}]}).status_code == 400
    for extra in ({"store": True}, {"stream": False}, {"url": "https://example.com"}, {"reasoning": {"effort": []}}):
        assert client.post("/api/chatgpt/responses", json={**BODY, **extra}).status_code == 422
    session.sign_out()
    assert client.post("/api/chatgpt/responses", json=BODY).status_code == 401
    assert len(responses.requests) == 1


def test_companion_endpoint_keeps_access_origin_and_loopback_guards(tmp_path):
    client, _, responses = bridge_client(tmp_path)
    for headers in ({"X-ESF-Token": ""}, {"Sec-Fetch-Site": "cross-site"},
                    {"Origin": "https://untrusted.example"}, {"X-Forwarded-For": "127.0.0.1"}):
        assert client.post("/api/chatgpt/responses", json=BODY, headers=headers).status_code in (401, 403)
    remote = TestClient(client.app, base_url=BASE, client=("192.0.2.1", 50000))
    assert remote.post("/api/chatgpt/responses", json=BODY, headers={"X-ESF-Token": ACCESS_TOKEN}).status_code == 403
    assert not responses.requests


def test_companion_auth_failure_invalidates_the_session(tmp_path):
    def revoked(request):
        return httpx.Response(401, json={"error": {"code": "invalid_token", "message": "revoked"}})

    client, session, _ = bridge_client(tmp_path, revoked)
    result = client.post("/api/chatgpt/responses", json=BODY)
    assert result.status_code == 401 and "Sign in with ChatGPT again" in result.json()["error"]["message"]
    assert session.state()["status"] != "connected"


def test_https_website_runs_on_the_subscription_through_an_approved_companion(browser, tmp_path, monkeypatch):
    """HTTPS website -> HTTP loopback popup -> local OAuth/API; literature/storage stay in the website."""
    with serving(tmp_path, monkeypatch, access_token=ACCESS_TOKEN) as helper:
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        network = Network()
        context.route("**/*", network)

        def hosted(route, request):
            path = urlparse(request.url).path.lstrip("/") or "index.html"
            mime = "text/javascript" if path.endswith((".js", ".mjs")) else "text/html"
            route.fulfill(path=str(SITE / path), content_type=mime)

        context.route("https://effect-size.example/**", hosted)

        def authorize(route, request):
            query = helper["sign_in"].authorize(request.url)
            route.fulfill(status=302, headers={"location": f"{helper['url']}auth/callback?{query}"})

        context.route("https://auth.openai.com/**", authorize)
        errors = []
        context.on("page", lambda page: page.on("pageerror", lambda error: errors.append(str(error))))
        local = context.new_page()
        local.goto(f"{helper['url']}#key={ACCESS_TOKEN}")
        local.wait_for_function("localStorage.getItem('esf-access-key') !== null")
        page = context.new_page()
        page.add_init_script("""window.messages = []; window.addEventListener('message', e => messages.push(e.data));
            const openWindow = window.open.bind(window);
            window.open = (...args) => { window.companion = openWindow(...args); return window.companion; };""")
        page.goto("https://effect-size.example/")
        page.check("input[name='provider'][value='chatgpt']")
        page.fill("#helper-address", helper["url"])
        with context.expect_page() as opened:
            page.click("#connect-chatgpt")
        companion = opened.value
        companion.wait_for_selector("#allow:enabled")
        assert companion.inner_text("#website") == "https://effect-size.example"
        assert page.get_attribute("#agents-toggle", "data-missing") == "true"
        assert page.evaluate("messages.every(m => m.kind === 'waiting')")
        # Requests before consent, from another source, or with the wrong nonce cannot spend usage.
        page.evaluate("""body => {
            const hello = messages[0];
            companion.postMessage({...hello, kind:'request', id:'before-consent', method:'responses', body}, '*');
            window.postMessage({...hello, kind:'state', state:{status:'connected', models:[]}}, location.origin);
        }""", BODY)
        page.wait_for_timeout(100)
        assert page.get_attribute("#agents-toggle", "data-missing") == "true" and not helper["responses"].requests
        companion.click("#allow")
        with context.expect_page() as signed:
            companion.click("#sign-in")
        signed.value.wait_for_url("**/auth/callback?**")
        assert signed.value.inner_text("h1") == "Signed in with ChatGPT"
        page.wait_for_selector("#agents-toggle[data-missing='false']")
        assert page.locator("#model-worker option").all_text_contents() == ["GPT-6.1 Sol", "GPT-6 Luna", "GPT-6 Mini"]
        page.select_option("#model-worker", "gpt-6-mini")
        page.evaluate("""body => {
            const hello = messages[0];
            companion.postMessage({...hello, channel:'wrong', kind:'request', id:'bad-nonce', method:'responses', body}, '*');
            companion.postMessage({...hello, kind:'request', id:'not-exposed', method:'sign-out'}, '*');
        }""", BODY)
        companion.evaluate("""body => window.dispatchEvent(new MessageEvent('message', {
            source: window, origin:'https://effect-size.example', data: {
                protocol:'esf-chatgpt-v1', channel:'wrong', kind:'request', id:'wrong-source', method:'responses', body}
        }))""", BODY)
        page.wait_for_timeout(100)
        assert not helper["responses"].requests
        page.click("#agents-done")
        page.fill("#contact-email", "test@example.com")
        page.dispatch_event("#contact-email", "change")
        page.click("#setup-done")
        page.fill("#paragraph", PARAGRAPH)
        page.click("#read")
        page.wait_for_selector("#spec:not([hidden])")
        page.click("#run")
        page.wait_for_selector("#tab-review[aria-selected='true']", timeout=30000)
        page.wait_for_selector("#review:not([hidden]) .paper")
        page.click("#tab-summary")
        page.click("#summarize")
        page.wait_for_selector("#summary-body .summary-overview", timeout=15000)
        requests = helper["responses"].requests
        assert requests and all(request.headers["authorization"] == "Bearer at-1" for request, _ in requests)
        assert {body["model"] for _, body in requests} == {"gpt-6.1-sol", "gpt-6-mini"}
        assert all(body["store"] is False and body["stream"] is True for _, body in requests)
        searches = [body for _, body in requests if "tools" in body]
        assert searches and all(body["tools"][0]["type"] == "namespace" for body in searches)
        assert any(item.get("type") == "reasoning" and item.get("encrypted_content")
                   for body in searches for item in body["input"])
        assert not network.claude and not network.openai_calls
        saved = page.evaluate("""async () => {
            const {Store} = await import('/lib/store.js');
            return {messages, local:{...localStorage}, session:{...sessionStorage}, projects:await new Store().projects()};
        }""")
        serialized = json.dumps(saved)
        assert all(secret not in serialized for secret in (ACCESS_TOKEN, "at-1", "rt-1"))
        assert saved["projects"][0]["status"] == "done"
        companion.click("#sign-out")
        page.wait_for_selector("#agents-toggle[data-missing='true']")
        assert companion.is_visible("#sign-in")
        with context.expect_page() as reconnected:
            companion.click("#sign-in")
        reconnected.value.wait_for_url("**/auth/callback?**")
        page.wait_for_selector("#agents-toggle[data-missing='false']")
        companion.close()
        page.wait_for_selector("#agents-toggle[data-missing='true']")
        page.reload()
        page.click("#agents-toggle")
        assert page.is_checked("input[name='provider'][value='chatgpt']")
        assert page.get_attribute("#agents-toggle", "data-missing") == "true"
        assert page.locator("#project-list li").count() == 1
        assert not errors, errors
        context.close()


def test_subscription_pdf_falls_back_to_its_text_layer(browser, site_url):
    page = browser.new_page()
    page.goto(site_url)
    result = page.evaluate("""async () => {
        const {ChatGPT} = await import('/lib/chatgpt.js');
        const {OpenAIError} = await import('/lib/openai.js');
        const calls = [];
        const connection = {request: async (method, body) => {
            calls.push(body);
            if (calls.length === 1) throw new OpenAIError('subscription_sharing_unsupported_capability', 'files unsupported');
            return {output:[{type:'message',content:[{type:'output_text',text:'{"ok":true}'}]}]};
        }};
        const llm = new ChatGPT({connection, models:{manager:'gpt-test',worker:'gpt-test'}});
        const pdf = {type:'document',title:'Trial',source:{type:'base64',media_type:'application/pdf',data:'Zg=='},text:'Measured change: -2.1'};
        const options = {manager:false,content:[pdf],schema:{type:'object'},name:'PaperExtraction',effort:'low'};
        const answer = await llm.parse(options);
        let missing;
        try { await llm.parse({...options,content:[{...pdf,text:''}]}); } catch (error) {missing = error.code;}
        return {calls,answer,missing};
    }""")
    assert result["answer"] == {"ok": True} and result["missing"] == "pdf_not_readable"
    assert result["calls"][0]["input"][0]["content"][0]["type"] == "input_file"
    assert result["calls"][1]["input"][0]["content"] == [{"type": "input_text", "text": "Document: Trial\n\nMeasured change: -2.1"}]
    page.close()


def test_the_manager_reads_a_pdf_in_its_text_layer_on_the_subscription(browser, site_url):
    """A ChatGPT plan that does not accept files: the manager's reading of a paper extracted from a PDF
    falls back to the PDF's text layer, as its extraction does."""
    page = browser.new_page()
    page.goto(site_url)
    paper = _paper(1, [("PHQ-9", "12 weeks", [("App", "mean", 8.1)])], ext_id="1001", extracted_by="worker", fulltext_kind="upload")
    project_id = stored_search(page, [paper])
    reading = {"design": "two-arm parallel RCT", "sample_size": "", "sample_size_location": "", "notes": "", "results": [{
        "result": 1, "population": "all randomized", "treatments": "app vs waitlist", "variable": "PHQ-9 at 12 weeks",
        "intercurrent_events": "not stated", "summary_measure": "difference in means", "model": "ANCOVA adjusted for baseline",
        "missing_data": "not stated", "quote": "Analyses followed the intention-to-treat principle.", "location": "Methods",
        "numbers_are": "observed means", "vs_plan": "same", "differences": [], "use": "The means give the difference."}]}
    result = page.evaluate("""async ([id, reading]) => {
        const { Store } = await import('/lib/store.js');
        const { Pipeline } = await import('/lib/pipeline.js');
        const { ChatGPT } = await import('/lib/chatgpt.js');
        const { OpenAIError } = await import('/lib/openai.js');
        const store = new Store();
        const paper = (await store.papers(id))[0];
        await store.saveDocument(id, paper.id, { kind: 'upload', url: 'trial-1.pdf', pdf: 'Zg==',
          text: 'Trial 1. Analyses followed the intention-to-treat principle.' });
        const calls = [];
        const connection = { request: async (method, body) => {
          calls.push(body);
          if (calls.length === 1) throw new OpenAIError('subscription_sharing_unsupported_capability', 'files unsupported');
          return { output: [{ type: 'message', content: [{ type: 'output_text', text: JSON.stringify(reading) }] }] };
        } };
        const llm = new ChatGPT({ connection, models: { manager: 'gpt-test', worker: 'gpt-test' } });
        await new Pipeline({ store, llm, sources: null }).readContext(id, paper.id);
        return { calls, context: (await store.paper(paper.id)).context };
    }""", [project_id, reading])
    first, second = (call["input"][0]["content"] for call in result["calls"])
    assert first[0]["type"] == "input_file" and result["calls"][0]["text"]["format"]["name"] == "StudyContext"
    assert second[0] == {"type": "input_text", "text": "Document: Trial 1\n\nTrial 1. Analyses followed the intention-to-treat principle."}
    context = result["context"]
    assert context["status"] == "done" and context["result"] == reading and context["model"] == "gpt-test"
    assert context["checks"] == {"sample_size": None, "results": ["exact"]}
    page.close()
