"""The local edition's page (web/) against the server, driven in Chromium: the Agents page and the
three ways to run the agents (Claude with an Anthropic API key, OpenAI's models with an OpenAI API
key, OpenAI's models on a ChatGPT plan). OpenAI's sign-in service and the Responses API are the test
fakes; the browser's trip to ChatGPT's sign-in page is answered with the redirect back to the
server's /auth/callback, as ChatGPT sends it."""

from __future__ import annotations

import glob
import os
import socket
import threading
import time
from urllib.parse import urlparse

import httpx
import pytest

from esf.chatgpt import ChatGPTSession
from esf.config import REPO_ROOT, Settings
from esf.openai_llm import OpenAILLM
from esf.server import create_app
from esf.sources import Sources
from esf.store import Store
from fixtures import FakeLLM, FakeOpenAI, LiteratureAPIs, mock_client
from test_chatgpt import CATALOG
from test_chatgpt import FakeOpenAI as FakeSignIn

sync_api = pytest.importorskip("playwright.sync_api")
uvicorn = pytest.importorskip("uvicorn")

ANTHROPIC_KEY = "sk-ant-api03-test-key-0123456789"
OPENAI_KEY = "sk-proj-test-key-0123456789abcdef"
NCBI_KEY = "0123456789abcdef0123456789abcdef0123"
PARAGRAPH = "We propose a two-arm randomized trial of a 12-week mindfulness app versus waitlist for PHQ-9 in students."


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture()
def served(tmp_path, monkeypatch):
    """The app on a loopback port, with no keys of its own, and OpenAI's sign-in and Responses API faked."""
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(data_dir=tmp_path, web_dir=REPO_ROOT / "web", contact_email="test@example.com", audit_fraction=0.0)
    sign_in = FakeSignIn()
    responses = FakeOpenAI()  # the Responses API on the plan
    api_responses = FakeOpenAI()  # the Responses API with an API key
    session = ChatGPTSession(tmp_path / "chatgpt.json", client=httpx.Client(transport=httpx.MockTransport(sign_in)))
    plans, keyed, openai = [], [], []

    def on_plan(chatgpt):
        chosen = chatgpt.state()
        plans.append((chosen["manager_model"], chosen["worker_model"]))
        return OpenAILLM(settings, chatgpt.access_token, chosen["manager_model"], chosen["worker_model"],
                         client=httpx.Client(transport=httpx.MockTransport(responses)), reasoning_levels=chatgpt.reasoning_levels)

    def on_key(key, manager, worker):
        keyed.append((key, manager, worker))
        return FakeLLM()

    def on_openai(key, manager, worker):
        openai.append((key, manager, worker))
        return OpenAILLM(settings, lambda: key, manager, worker, client=httpx.Client(transport=httpx.MockTransport(api_responses)),
                         namespaced_tools=False)

    app = create_app(settings, llm=FakeLLM(), sources=Sources(settings, client=mock_client(LiteratureAPIs())),
                     store=Store(tmp_path / "test.sqlite3"), chatgpt=session, llm_for_chatgpt=on_plan, llm_for_key=on_key,
                     llm_for_openai=on_openai)
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    yield {"url": f"http://127.0.0.1:{port}/", "port": port, "sign_in": sign_in, "responses": responses, "api_responses": api_responses,
           "plans": plans, "keyed": keyed, "openai": openai, "path": tmp_path}
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def browser():
    executable = os.environ.get("ESF_CHROMIUM") or next(iter(sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux/chrome"))[::-1]), None)
    with sync_api.sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=executable)
        yield b
        b.close()


def open_page(browser, served):
    """A page on the app, with its errors and the headers of the requests that start jobs."""
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    page.errors, page.jobs = [], []
    page.on("pageerror", lambda e: page.errors.append(str(e)))
    page.on("console", lambda m: page.errors.append(m.text) if m.type == "error" else None)
    page.on("request", lambda r: page.jobs.append(r.headers) if r.method == "POST" and "/api/projects" in r.url else None)
    page.goto(served["url"])
    return context, page


def card(page, provider):
    return page.locator(f".provider-card:has(input[value='{provider}'])")


def options(page, select):
    return page.locator(f"{select} option").all_inner_texts()


def test_the_agents_page_comes_first_until_a_provider_is_ready(browser, served):
    context, page = open_page(browser, served)
    page.wait_for_selector("#agents-page:not([hidden])")
    assert page.is_hidden("#intro") and page.is_hidden("#searches")
    toggle = page.locator("#agents-toggle")
    assert toggle.inner_text() == "Agents: Claude API"
    assert toggle.get_attribute("data-missing") == "true" and toggle.get_attribute("aria-expanded") == "true"

    # The three ways, each with what it costs and the models it runs.
    claude, openai, plan = (card(page, p).inner_text() for p in ("anthropic", "openai", "chatgpt"))
    assert claude.startswith("Claude API") and "Anthropic API key · billed per use" in claude and "Manager Opus 5.5 · Workers Haiku 5.5" in claude
    assert openai.startswith("OpenAI API") and "OpenAI API key · billed per use" in openai and "Manager GPT-6.1 Sol · Workers GPT-6 Luna" in openai
    assert plan.startswith("OpenAI subscription") and "ChatGPT Plus or Pro · no key" in plan
    assert page.locator(".role .role-name").all_inner_texts() == ["Manager", "Workers", "Code"]  # the server downloads: no fetcher

    # Each card, clicked anywhere, shows its own way in and its own models.
    boxes = {"anthropic": "#anthropic-box", "openai": "#openai-box", "chatgpt": "#chatgpt-box"}
    for provider, label in (("openai", "OpenAI API"), ("chatgpt", "OpenAI subscription"), ("anthropic", "Claude API")):
        card(page, provider).locator(".provider-billing").click()
        assert page.is_checked(f"input[name='provider'][value='{provider}']")
        assert [page.is_visible(box) for box in boxes.values()] == [p == provider for p in boxes]
        assert toggle.inner_text() == f"Agents: {label}"
        if provider == "openai":
            assert options(page, "#model-manager") == options(page, "#model-worker") == ["GPT-6.1 Sol", "GPT-6 Luna"]
            assert (page.input_value("#model-manager"), page.input_value("#model-worker")) == ("gpt-6.1-sol", "gpt-6-luna")
            assert "OpenAI's API" in page.inner_text("#agents-privacy")
        elif provider == "chatgpt":
            assert page.is_disabled("#model-manager") and page.is_disabled("#model-worker")
            assert "Sign in with ChatGPT to choose" in page.inner_text("#roles-note")
        else:
            assert options(page, "#model-manager") == ["Opus 5.5", "Sonnet 5.5", "Haiku 5.5"]
            assert (page.input_value("#model-manager"), page.input_value("#model-worker")) == ("claude-opus-5-5", "claude-haiku-5-5")
            assert "Anthropic's API" in page.inner_text("#agents-privacy")

    # Another vendor's key, or an NCBI key, is not taken for an Anthropic one.
    page.fill("#api-key", OPENAI_KEY)
    assert page.inner_text("#key-status").startswith("This is an OpenAI API key, not an Anthropic API key")
    assert toggle.get_attribute("data-missing") == "true" and card(page, "anthropic").locator(".provider-ready").is_hidden()
    page.fill("#api-key", NCBI_KEY)
    assert "NCBI API key" in page.inner_text("#key-status") and toggle.get_attribute("data-missing") == "true"
    # A key is never kept as the other vendor's, and following the advice carries it over to its own box.
    page.fill("#api-key", OPENAI_KEY)
    kept = "() => [sessionStorage.getItem('esf-anthropic-api-key'), sessionStorage.getItem('esf-openai-api-key')]"
    assert page.evaluate(kept) == [None, None]
    card(page, "openai").click()
    assert (page.input_value("#openai-key"), page.input_value("#api-key")) == (OPENAI_KEY, "")
    assert page.evaluate(kept) == [None, OPENAI_KEY]
    page.fill("#openai-key", ANTHROPIC_KEY)
    assert page.inner_text("#openai-key-status").startswith("This is an Anthropic API key, not an OpenAI API key")
    card(page, "anthropic").click()
    assert (page.input_value("#api-key"), page.input_value("#openai-key")) == (ANTHROPIC_KEY, "")
    assert page.evaluate(kept) == [ANTHROPIC_KEY, None]
    assert page.inner_text("#key-status").startswith("Kept for this tab only.")
    assert toggle.get_attribute("data-missing") == "false" and card(page, "anthropic").locator(".provider-ready").is_visible()

    page.click("#agents-done")
    assert page.is_hidden("#agents-page") and page.is_visible("#intro") and page.is_visible("#searches")
    assert toggle.get_attribute("aria-expanded") == "false"
    # With a key the page opens on the searches; the header opens the Agents page again.
    with page.expect_response("**/api/projects"):
        page.reload()
    assert page.is_hidden("#agents-page") and page.is_visible("#searches")
    page.click("#agents-toggle")
    assert page.is_visible("#agents-page") and page.is_hidden("#intro")
    # Escape closes it and gives the focus back to its button.
    page.keyboard.press("Escape")
    assert page.is_hidden("#agents-page") and page.evaluate("document.activeElement.id") == "agents-toggle"
    assert not page.errors, page.errors
    context.close()


def test_the_openai_api_with_a_key_from_the_page(browser, served):
    context, page = open_page(browser, served)
    page.wait_for_selector("#agents-page:not([hidden])")
    card(page, "openai").click()
    assert "Create one in the OpenAI dashboard" in page.inner_text("#openai-key-status")
    page.fill("#openai-key", OPENAI_KEY)
    assert page.inner_text("#openai-key-status").startswith("Kept for this tab only. Sent only with requests that run OpenAI's models")
    page.select_option("#model-manager", "gpt-6-luna")
    page.select_option("#model-worker", "gpt-6.1-sol")
    assert page.inner_text("#models").startswith("GPT-6 Luna plans and reviews · GPT-6.1 Sol screens and reads")
    assert "Manager GPT-6 Luna · Workers GPT-6.1 Sol" in card(page, "openai").inner_text()
    # The OpenAI key is kept apart from the Anthropic one, for this tab only; the models on this browser.
    kept = page.evaluate("""() => [sessionStorage.getItem('esf-openai-api-key'), localStorage.getItem('esf-openai-api-key'),
        sessionStorage.getItem('esf-anthropic-api-key'), localStorage.getItem('esf-anthropic-api-key'), JSON.parse(localStorage.getItem('esf-models-openai'))]""")
    assert kept == [OPENAI_KEY, None, None, None, {"manager": "gpt-6-luna", "worker": "gpt-6.1-sol"}]
    page.click("#agents-done")
    assert "sent to OpenAI's API" in page.inner_text("#intro .notice")

    page.fill("#paragraph", PARAGRAPH)
    page.click("#read")
    page.wait_for_selector("#spec:not([hidden])")
    assert "How GPT-6 Luna read your paragraph" in page.inner_text("#spec .summary")
    page.click("#run")
    page.wait_for_selector("#map-body .map-stage[data-stage='ready'][data-state='done']", timeout=30000)
    page.wait_for_selector("#review:not([hidden]) .paper")

    # The server was told the provider, the key and the models; nothing went in Anthropic's header.
    assert len(page.jobs) == 2
    for headers in page.jobs:
        assert headers["x-model-provider"] == "openai" and headers["x-openai-api-key"] == OPENAI_KEY and "x-anthropic-api-key" not in headers
        assert (headers["x-manager-model"], headers["x-worker-model"]) == ("gpt-6-luna", "gpt-6.1-sol")
    assert served["openai"] == [(OPENAI_KEY, "gpt-6-luna", "gpt-6.1-sol")] * 2 and served["keyed"] == [] and served["plans"] == []
    # OpenAI got the key as a bearer token and the search agent's tools as plain function tools.
    requests = served["api_responses"].requests
    assert requests and all(request.headers["authorization"] == f"Bearer {OPENAI_KEY}" for request, _ in requests)
    assert {body["model"] for _, body in requests} == {"gpt-6-luna", "gpt-6.1-sol"}
    tools = [body["tools"] for _, body in requests if "tools" in body]
    assert tools and all(tool["type"] == "function" and tool["strict"] for listed in tools for tool in listed)
    assert [tool["name"] for tool in tools[0]] == ["count_pubmed", "search_pubmed", "search_europepmc", "search_clinicaltrials", "submit_search_plan"]
    assert served["responses"].requests == []
    assert page.locator("#map-body [data-stage='extract'] .map-agent").all_inner_texts() == ["Code", "GPT-6.1 Sol", "GPT-6 Luna"]
    # Choosing Claude now changes nothing about this search: its work stays credited to the models that did it.
    page.click("#agents-toggle")
    card(page, "anthropic").click()
    page.click("#agents-done")
    assert page.inner_text("#models").startswith("Opus 5.5 plans and reviews")
    assert "How GPT-6 Luna read your paragraph" in page.inner_text("#spec .summary")
    assert page.locator("#map-body [data-stage='extract'] .map-agent").all_inner_texts() == ["Code", "GPT-6.1 Sol", "GPT-6 Luna"]
    assert not page.errors, page.errors
    context.close()


def test_claude_gets_only_the_anthropic_key(browser, served):
    context, page = open_page(browser, served)
    page.wait_for_selector("#agents-page:not([hidden])")
    # An OpenAI key entered too stays with OpenAI.
    card(page, "openai").click()
    page.fill("#openai-key", OPENAI_KEY)
    card(page, "anthropic").click()
    page.fill("#api-key", ANTHROPIC_KEY)
    page.select_option("#model-manager", "claude-sonnet-5-5")
    page.click("#agents-done")
    page.fill("#paragraph", PARAGRAPH)
    page.click("#read")
    page.wait_for_selector("#spec:not([hidden])")
    assert "How Sonnet 5.5 read your paragraph" in page.inner_text("#spec .summary")
    project_id = page.evaluate("location.hash.slice(1)")
    [headers] = page.jobs
    assert headers["x-model-provider"] == "anthropic" and headers["x-anthropic-api-key"] == ANTHROPIC_KEY and "x-openai-api-key" not in headers
    assert (headers["x-manager-model"], headers["x-worker-model"]) == ("claude-sonnet-5-5", "claude-haiku-5-5")
    assert served["keyed"] == [(ANTHROPIC_KEY, "claude-sonnet-5-5", "claude-haiku-5-5")] and served["openai"] == []

    # Remembered on this browser, then forgotten: a job then opens the Agents page and says why.
    page.click("#agents-toggle")
    page.check("#remember-key")
    kept = page.evaluate("() => [localStorage.getItem('esf-anthropic-api-key'), sessionStorage.getItem('esf-anthropic-api-key')]")
    assert kept == [ANTHROPIC_KEY, None]
    page.click("#forget-key")
    assert page.evaluate("() => [localStorage.getItem('esf-anthropic-api-key'), sessionStorage.getItem('esf-anthropic-api-key')]") == [None, None]
    assert page.get_attribute("#agents-toggle", "data-missing") == "true"
    page.click("#agents-done")
    page.click("#new-project")
    page.fill("#paragraph", PARAGRAPH)
    page.click("#read")
    assert page.is_visible("#agents-page") and page.is_hidden("#searches")
    assert page.inner_text("#key-status") == "Enter your Anthropic API key first."
    assert page.evaluate("document.activeElement.id") == "api-key" and len(page.jobs) == 1

    # A link to a search opens the search, though no key is set; the Agents button says one is missing.
    page.goto(f"{served['url']}#{project_id}")
    page.reload()
    page.wait_for_selector("#spec:not([hidden])")
    assert page.is_hidden("#agents-page") and page.get_attribute("#agents-toggle", "data-missing") == "true"
    assert not page.errors, page.errors
    context.close()


def test_sign_in_with_chatgpt_and_search_on_the_plan(browser, served):
    sign_in, responses = served["sign_in"], served["responses"]
    context, page = open_page(browser, served)
    waiting = {"on": True}

    def chatgpt_sign_in_page(route, request):
        # The person signs in; ChatGPT sends the browser back to the callback on 127.0.0.1. Or, at
        # first, the person leaves the ChatGPT tab as it is.
        assert urlparse(request.url).hostname == "auth.openai.com"
        if waiting["on"]:
            route.fulfill(status=200, content_type="text/html", body="<h1>Sign in to ChatGPT</h1>")
            return
        callback = f"http://127.0.0.1:{served['port']}/auth/callback?{sign_in.authorize(request.url)}"
        route.fulfill(status=302, headers={"location": callback})

    context.route("https://auth.openai.com/**", chatgpt_sign_in_page)
    page.wait_for_selector("#agents-page:not([hidden])")
    card(page, "chatgpt").click()
    assert page.is_hidden("#anthropic-box") and page.is_visible("#chatgpt-box")
    assert "Plus or Pro" in page.inner_text("#chatgpt-status")
    # Without a sign-in the page asks for one instead of starting.
    page.click("#agents-done")
    page.fill("#paragraph", PARAGRAPH)
    page.click("#read")
    assert page.is_visible("#agents-page") and page.inner_text("#chatgpt-status") == "Sign in with ChatGPT first."
    assert page.get_attribute("#agents-toggle", "data-missing") == "true" and page.jobs == []

    # A sign-in left waiting on the ChatGPT tab can be given up.
    with context.expect_page() as opened:
        page.click("#chatgpt-sign-in")
    opened.value.wait_for_url("https://auth.openai.com/**")
    page.wait_for_selector("#chatgpt-cancel:not([hidden])")
    assert page.inner_text("#chatgpt-status").startswith("Finish signing in on the ChatGPT tab")
    page.click("#chatgpt-cancel")
    page.wait_for_selector("#chatgpt-cancel", state="hidden")
    assert page.inner_text("#chatgpt-status").startswith("Opens ChatGPT in a new tab")
    opened.value.close()

    waiting["on"] = False
    with context.expect_page() as opened:
        page.click("#chatgpt-sign-in")
    tab = opened.value
    tab.wait_for_url("**/auth/callback?**")
    tab.wait_for_load_state()
    assert tab.inner_text("h1") == "Signed in with ChatGPT"
    assert tab.url.startswith(f"http://127.0.0.1:{served['port']}/auth/callback?code=")
    page.wait_for_function("document.querySelector('#chatgpt-status').textContent.startsWith('Signed in as')")
    assert page.inner_text("#chatgpt-status").startswith("Signed in as Ada Lovelace (ada@example.com).")
    assert page.get_attribute("#agents-toggle", "data-missing") == "false"
    authorization = sign_in.authorizations[0]
    assert authorization["redirect_uri"] == f"http://127.0.0.1:{served['port']}/auth/callback"
    assert authorization["agent_name_hint"] == "Effect Size Finder"
    # The roles take the plan's models; the default choice is OpenAI's lead model and its efficient one.
    assert options(page, "#model-manager") == options(page, "#model-worker") == ["GPT-6.1 Sol", "GPT-6 Luna", "GPT-6 Mini"]
    assert page.input_value("#model-manager") == "gpt-6.1-sol" and page.input_value("#model-worker") == "gpt-6-luna"
    # A chosen model the plan stops offering stays chosen, marked, until another is picked.
    sign_in.catalog = {"models": [m for m in CATALOG["models"] if not (isinstance(m, dict) and m.get("slug") == "gpt-6-luna")]}
    card(page, "anthropic").click()
    with page.expect_response("**/api/chatgpt?refresh=true"):
        card(page, "chatgpt").click()
    page.wait_for_function("document.querySelector('#model-worker option').textContent.endsWith('(not on your plan)')")
    assert options(page, "#model-worker") == ["GPT-6 Luna (not on your plan)", "GPT-6.1 Sol", "GPT-6 Mini"]
    assert options(page, "#model-manager") == ["GPT-6.1 Sol", "GPT-6 Mini"]
    page.select_option("#model-worker", "gpt-6-mini")
    page.wait_for_function("document.querySelector('#models').textContent.includes('GPT-6 Mini screens')")
    assert page.inner_text("#models").startswith("GPT-6.1 Sol plans and reviews · GPT-6 Mini screens and reads")
    assert "Manager GPT-6.1 Sol · Workers GPT-6 Mini" in card(page, "chatgpt").inner_text()
    # Only the registration and the choice of models are kept on disk; the tokens stay in memory.
    saved = (served["path"] / "chatgpt.json").read_text()
    assert "app_Test123" in saved and "gpt-6-mini" in saved and "at-1" not in saved and "rt-1" not in saved
    assert "on your ChatGPT plan" in page.inner_text("#agents-privacy")

    page.click("#agents-done")
    page.click("#read")
    page.wait_for_selector("#spec:not([hidden])")
    assert "How GPT-6.1 Sol read your paragraph" in page.inner_text("#spec .summary")
    # When the run ends the page asks again about the sign-in, which may have expired meanwhile.
    with page.expect_request(lambda request: request.url.endswith("/api/chatgpt"), timeout=30000):
        page.click("#run")
        page.wait_for_selector("#map-body .map-stage[data-stage='ready'][data-state='done']", timeout=30000)
    page.wait_for_selector("#review:not([hidden]) .paper")
    assert served["plans"] and set(served["plans"]) == {("gpt-6.1-sol", "gpt-6-mini")}
    # A plan's jobs carry no key and no models: the server has the sign-in and its choice.
    assert len(page.jobs) == 2 and all(headers["x-model-provider"] == "chatgpt" for headers in page.jobs)
    assert not any(name in headers for headers in page.jobs for name in ("x-anthropic-api-key", "x-openai-api-key", "x-manager-model"))
    models = {body["model"] for _, body in responses.requests}
    assert models == {"gpt-6.1-sol", "gpt-6-mini"}
    assert all(request.headers["authorization"] == "Bearer at-1" for request, _ in responses.requests)
    # The catalog says GPT-6.1 Sol reasons at low or high: adjudication's xhigh comes down to high.
    efforts = {body["reasoning"]["effort"] for _, body in responses.requests if body["model"] == "gpt-6.1-sol"}
    assert efforts == {"high"}
    assert [c.get_attribute("data-agent") for c in page.locator("#map-body [data-stage='extract'] .map-agent").all()] == ["code", "worker", "manager"]
    assert page.locator("#map-body [data-stage='extract'] .map-agent").all_inner_texts() == ["Code", "GPT-6 Mini", "GPT-6.1 Sol"]
    page.locator("#papers .paper-details summary").first.click()
    assert page.locator("#papers .badge", has_text="Extracted by GPT-6 Mini").count() + page.locator(
        "#papers .badge", has_text="Reviewed by GPT-6.1 Sol").count() == 1

    page.click("#agents-toggle")
    page.click("#chatgpt-sign-out")
    page.wait_for_function("!document.querySelector('#chatgpt-sign-in').hidden")
    assert sign_in.revocations and sign_in.revocations[-1]["token"] == "rt-1"
    assert page.is_disabled("#model-manager")
    page.click("#agents-done")
    page.click("#summarize")
    assert page.is_visible("#agents-page") and page.inner_text("#chatgpt-status") == "Sign in with ChatGPT first."
    assert not page.errors, page.errors
    context.close()


def test_a_job_on_the_plan_needs_a_sign_in(served):
    response = httpx.post(served["url"] + "api/projects", json={"paragraph": "A randomized trial of a mindfulness app for PHQ-9."},
                          headers={"X-Model-Provider": "chatgpt"})
    assert response.status_code == 409 and response.json()["detail"] == "Sign in with ChatGPT first, on the Agents page."
    assert httpx.post(served["url"] + "api/projects", json={"paragraph": "A randomized trial of a mindfulness app for PHQ-9."},
                      headers={"X-Model-Provider": "gemini"}).status_code == 400
    state = httpx.get(served["url"] + "api/chatgpt").json()
    assert state["status"] == "disconnected" and state["remote"] is False and "token" not in str(state).lower()
    # A callback that does not belong to a sign-in this server started changes nothing.
    page = httpx.get(served["url"] + "auth/callback?code=abc&state=forged")
    assert page.status_code == 400 and "did not finish" in page.text and page.headers["cache-control"] == "no-store"
    assert httpx.get(served["url"] + "api/chatgpt").json()["status"] == "disconnected"
