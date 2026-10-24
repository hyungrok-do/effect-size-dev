"""The local edition's page (web/) against the server, driven in Chromium: the Agents page and the
three ways to run the agents (Claude with an Anthropic API key, OpenAI's models with an OpenAI API
key, OpenAI's models on a ChatGPT plan), and the server's access key. OpenAI's sign-in service and the
Responses API are the test fakes; the browser's trip to ChatGPT's sign-in page is answered with the
redirect back to the server's /auth/callback, as ChatGPT sends it."""

from __future__ import annotations

import contextlib
import glob
import json
import os
import socket
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pytest

from esf.chatgpt import (
    ANOTHER_ACCOUNT, APP_NAME, NO_CALLBACK, PENDING_SECONDS, REFRESH_NOT_READY, REFRESH_WAIT, REGISTRATION_ENTRY, SCOPES, ChatGPTSession,
)
from esf.config import REPO_ROOT, Settings
from esf.openai_llm import OpenAILLM
from esf.server import create_app
from esf.sources import Sources
from esf.store import Store
from fixtures import FakeLLM, FakeOpenAI, LiteratureAPIs, mock_client
from test_chatgpt import CATALOG, WITHOUT_PLAN, as_bob
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


ACCESS_TOKEN = "test-access-key-0123456789abcdef"
ANOTHER_HINT = "To use another account, sign out of ChatGPT in this browser first, or switch accounts on ChatGPT's screen."


@contextlib.contextmanager
def serving(tmp_path, monkeypatch, access_token=None, allowed_hosts=()):
    """The app on a loopback port, with no keys of its own, and OpenAI's sign-in and Responses API faked.
    "skew" moves the sign-in's clock on."""
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(data_dir=tmp_path, web_dir=REPO_ROOT / "web", contact_email="test@example.com", audit_fraction=0.0,
                        allowed_hosts=allowed_hosts)
    sign_in = FakeSignIn()
    responses = FakeOpenAI()  # the Responses API on the plan
    api_responses = FakeOpenAI()  # the Responses API with an API key
    skew = [0.0]
    session = ChatGPTSession(tmp_path / "chatgpt.json", client=httpx.Client(transport=httpx.MockTransport(sign_in)),
                             clock=lambda: time.time() + skew[0])
    plans, keyed, openai = [], [], []

    def on_plan(chatgpt):
        chosen = chatgpt.state()
        plans.append((chosen["manager_model"], chosen["worker_model"]))
        return OpenAILLM(settings, chatgpt.access_token, chosen["manager_model"], chosen["worker_model"],
                         client=httpx.Client(transport=httpx.MockTransport(responses)), reasoning_levels=chatgpt.reasoning_levels,
                         on_error=chatgpt.handle_api_error)

    def on_key(key, manager, worker):
        keyed.append((key, manager, worker))
        return FakeLLM()

    def on_openai(key, manager, worker):
        openai.append((key, manager, worker))
        return OpenAILLM(settings, lambda: key, manager, worker, client=httpx.Client(transport=httpx.MockTransport(api_responses)),
                         namespaced_tools=False)

    app = create_app(settings, llm=FakeLLM(), sources=Sources(settings, client=mock_client(LiteratureAPIs())),
                     store=Store(tmp_path / "test.sqlite3"), chatgpt=session, llm_for_chatgpt=on_plan, llm_for_key=on_key,
                     llm_for_openai=on_openai, access_token=access_token)
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    try:
        yield {"url": f"http://127.0.0.1:{port}/", "port": port, "sign_in": sign_in, "responses": responses, "api_responses": api_responses,
               "plans": plans, "keyed": keyed, "openai": openai, "path": tmp_path, "skew": skew}
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@pytest.fixture()
def served(tmp_path, monkeypatch):
    with serving(tmp_path, monkeypatch) as app:
        yield app


def chromium() -> str | None:
    return os.environ.get("ESF_CHROMIUM") or next(iter(sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux/chrome"))[::-1]), None)


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=chromium())
        yield b
        b.close()


def open_page(browser, served, at="", url=None):
    """A page on the app (at `url`, else the server's own address, followed by `at`), with its errors,
    the headers of the requests that start jobs, and the API's requests and answers."""
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    page.errors, page.jobs, page.api = [], [], []
    page.on("pageerror", lambda e: page.errors.append(str(e)))
    page.on("console", lambda m: page.errors.append(m.text) if m.type == "error" else None)
    page.on("request", lambda r: page.jobs.append(r.headers) if r.method == "POST" and "/api/projects" in r.url else None)
    page.on("response", lambda r: page.api.append((r.request.method, r.url, r.request.headers, r.status)) if "/api/" in r.url else None)
    page.goto((url or served["url"]) + at)
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
    assert "set OPENAI_API_KEY where the server runs" in page.inner_text("#remember-openai-key-note")
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
    # Beside the box: who else can read a remembered key, and the lasting way to give the server one.
    note = page.inner_text("#remember-key-note")
    assert f"kept on this browser for {served['url'].rstrip('/')}, and any page served at that address later can read it." in note
    assert note.endswith("For a key that should last, set ANTHROPIC_API_KEY where the server runs instead.")
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


def test_rejected_catalog_credentials_show_the_sign_in_action(browser, served):
    sign_in = served["sign_in"]
    authorization = httpx.post(served["url"] + "api/chatgpt/sign-in").json()["url"]
    callback = served["url"] + "auth/callback?" + sign_in.authorize(authorization)
    assert httpx.get(callback).status_code == 200
    context, page = open_page(browser, served)
    page.wait_for_selector("#agents-page:not([hidden])")
    card(page, "chatgpt").click()
    page.wait_for_function("document.querySelector('#chatgpt-status').textContent.startsWith('Signed in as')")
    assert page.is_hidden("#chatgpt-sign-in")
    sign_in.catalog_status, sign_in.catalog = 401, {"error": {"code": "invalid_token"}}
    card(page, "anthropic").click()
    with page.expect_response("**/api/chatgpt?refresh=true"):
        card(page, "chatgpt").click()
    page.wait_for_selector("#chatgpt-sign-in:not([hidden])")
    assert page.inner_text("#chatgpt-sign-in") == "Sign in with ChatGPT again"
    assert page.is_disabled("#model-manager") and page.is_disabled("#model-worker")
    assert "expired or was revoked" in page.inner_text("#chatgpt-status")
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


def chatgpt_signs_in(sign_in, port, waiting=None):
    """ChatGPT's sign-in page, as a route: it sends the browser back to the callback at once, or, while
    waiting["on"], shows itself and waits for the person."""

    def route(route, request):
        if waiting and waiting["on"]:
            route.fulfill(status=200, content_type="text/html", body="<h1>Sign in to ChatGPT</h1>")
            return
        route.fulfill(status=302, headers={"location": f"http://127.0.0.1:{port}/auth/callback?{sign_in.authorize(request.url)}"})

    return route


def test_the_page_needs_the_servers_access_key(browser, tmp_path, monkeypatch):
    with serving(tmp_path, monkeypatch, access_token=ACCESS_TOKEN) as served:
        context, page = open_page(browser, served)
        # Opened without the key: the page says where to find it, and shows nothing else.
        page.wait_for_selector("#access-notice:not([hidden])")
        assert "Open the address that esf serve printed in the terminal; it carries this server's access key." in page.inner_text("#access-notice")
        assert all(page.is_hidden(part) for part in ("#intro", "#searches", "#agents-page", "#agents-toggle"))
        assert page.api and all(status == 401 for *_, status in page.api)
        assert not [e for e in page.errors if "401" not in e], page.errors

        # The printed address opened in the same tab: only its #part changes, and the page comes in.
        page.errors.clear()
        page.evaluate(f"location.hash = '#key={ACCESS_TOKEN}'")
        page.wait_for_selector("#agents-page:not([hidden])")
        assert page.is_hidden("#access-notice") and page.evaluate("location.href") == served["url"]  # the key is gone from it
        assert page.evaluate("localStorage.getItem('esf-access-key')") == ACCESS_TOKEN
        # Kept for this address, it opens the page again after a reload.
        page.reload()
        page.wait_for_selector("#agents-page:not([hidden])")
        page.fill("#api-key", ANTHROPIC_KEY)
        page.click("#agents-done")
        page.fill("#paragraph", PARAGRAPH)
        page.click("#read")
        page.wait_for_selector("#spec:not([hidden])")
        page.click("#run")
        page.wait_for_selector("#map-body .map-stage[data-stage='ready'][data-state='done']", timeout=30000)
        page.wait_for_selector("#review:not([hidden]) .paper")
        project_id = page.evaluate("location.hash.slice(1)")
        answered = [entry for entry in page.api if entry[3] != 401]
        assert answered and all(headers.get("x-esf-token") == ACCESS_TOKEN for _, _, headers, _ in answered)

        # The exports are fetched with the key and saved under the server's names.
        page.click("#review .exports-box summary")
        files = {}
        for button in ("#export-included", "#export-all", "#export-json"):
            with page.expect_download() as saved:
                page.click(button)
            files[saved.value.suggested_filename] = Path(saved.value.path()).read_text()
        included, everything, as_json = (f"effect-sizes-{project_id}{end}" for end in ("-included.csv", ".csv", ".json"))
        assert set(files) == {included, everything, as_json}
        assert files[everything].startswith("paper_id,source,id,") and len(files[everything].splitlines()) > 1
        assert files[included].splitlines() == files[everything].splitlines()[:1]  # nothing included yet
        assert json.loads(files[as_json])["project"]["id"] == project_id

        # ChatGPT's return to /auth/callback is an ordinary visit, with no key: the sign-in still finishes.
        context.route("https://auth.openai.com/**", chatgpt_signs_in(served["sign_in"], served["port"]))
        page.click("#agents-toggle")
        card(page, "chatgpt").click()
        with context.expect_page() as opened:
            page.click("#chatgpt-sign-in")
        opened.value.wait_for_url("**/auth/callback?**")
        opened.value.wait_for_load_state()
        assert opened.value.inner_text("h1") == "Signed in with ChatGPT"
        page.wait_for_function("document.querySelector('#chatgpt-status').textContent.startsWith('Signed in as')")
        assert not page.errors, page.errors
        context.close()

        # A link to a search behind the key opens that search; the key leaves the address, the search stays.
        context, page = open_page(browser, served, at=f"#key={ACCESS_TOKEN}&{project_id}")
        page.wait_for_selector("#spec:not([hidden])")
        assert page.evaluate("location.hash") == f"#{project_id}" and page.is_hidden("#access-notice")
        assert not page.errors, page.errors
        context.close()
        # Outside the browser, the API wants the key too.
        assert httpx.get(served["url"] + "api/projects").status_code == 401
        assert httpx.get(served["url"] + "api/projects", headers={"X-ESF-Token": ACCESS_TOKEN}).json()[0]["id"] == project_id


def test_keys_are_not_taken_on_a_page_open_over_plain_http(browser, tmp_path, monkeypatch):
    # esf.lan stands for the server's name on the network: http://esf.lan is not a secure context.
    with serving(tmp_path, monkeypatch, allowed_hosts=("esf.lan",)) as served:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-server")
        lan = browser.browser_type.launch(executable_path=chromium(), args=["--host-resolver-rules=MAP esf.lan 127.0.0.1"])
        address = f"http://esf.lan:{served['port']}/"
        try:
            context, page = open_page(lan, served, url=address)
            page.wait_for_selector("#searches:not([hidden])")
            assert page.evaluate("window.isSecureContext") is False
            # A key remembered for this address earlier is neither shown nor sent.
            page.evaluate(f"localStorage.setItem('esf-anthropic-api-key', '{ANTHROPIC_KEY}')")
            page.reload()
            page.click("#agents-toggle")
            assert all(page.is_disabled(part) for part in ("#api-key", "#remember-key", "#forget-key")) and page.input_value("#api-key") == ""
            status = page.inner_text("#key-status")
            assert status.startswith("This page isn't on a secure connection, so a key typed here could be read on the network")
            assert status.endswith("Meanwhile the server's own key is used.") and page.is_hidden("#remember-key-note")
            page.click("#agents-done")
            page.fill("#paragraph", PARAGRAPH)
            page.click("#read")
            page.wait_for_selector("#spec:not([hidden])")
            [headers] = page.jobs
            assert "x-anthropic-api-key" not in headers and served["keyed"] == []
            # The server refuses a key sent from here all the same.
            sent = page.evaluate("""() => fetch('/api/projects', {method: 'POST', body: JSON.stringify({paragraph: 'A randomized trial of an app for PHQ-9.'}),
                headers: {'Content-Type': 'application/json', 'X-Anthropic-Api-Key': 'sk-ant-api03-typed-here'}}).then(async (r) => [r.status, (await r.json()).detail])""")
            assert sent == [400, "This server takes API keys from other computers only over HTTPS."]

            # Without a key of the server's, the page says how to give it one.
            monkeypatch.delenv("ANTHROPIC_API_KEY")
            page.goto(address)
            page.wait_for_selector("#agents-page:not([hidden])")
            assert page.get_attribute("#agents-toggle", "title") == "Open this page over HTTPS to enter your Anthropic API key."
            assert page.inner_text("#key-status").endswith("or at http://127.0.0.1 on the computer that runs the server.")
            assert not [e for e in page.errors if "400" not in e], page.errors
            context.close()
        finally:
            lan.close()


def test_another_chatgpt_account_and_what_the_page_says_about_the_sign_in(browser, served):
    sign_in = served["sign_in"]
    context, page = open_page(browser, served)
    waiting = {"on": True}
    context.route("https://auth.openai.com/**", chatgpt_signs_in(sign_in, served["port"], waiting))
    page.wait_for_selector("#agents-page:not([hidden])")
    card(page, "chatgpt").click()
    # With no account to switch from, there is no other account to use yet.
    assert page.text_content("#chatgpt-another") == ANOTHER_ACCOUNT
    assert page.is_hidden("#chatgpt-another") and page.is_hidden("#chatgpt-another-hint")

    # A ChatGPT tab that never comes back: after ten minutes the page says what to try, and shows it.
    with context.expect_page() as opened:
        page.click("#chatgpt-sign-in")
    opened.value.wait_for_url("https://auth.openai.com/**")
    served["skew"][0] = PENDING_SECONDS + 1
    page.wait_for_function("document.querySelector('#chatgpt-status').textContent.includes(\"didn't come back\")")
    assert page.inner_text("#chatgpt-status").endswith(NO_CALLBACK)
    assert page.is_visible("#chatgpt-sign-in") and page.is_visible("#chatgpt-another")
    assert page.inner_text("#chatgpt-another-hint") == ANOTHER_HINT and page.is_visible("#chatgpt-another-hint")
    served["skew"][0] = 0.0
    opened.value.close()

    # ChatGPT signs Ada in without letting the app use her plan.
    waiting["on"] = False
    sign_in.scope = WITHOUT_PLAN
    with context.expect_page() as opened:
        page.click("#chatgpt-sign-in")
    opened.value.wait_for_url("**/auth/callback?**")
    opened.value.wait_for_load_state()
    assert opened.value.inner_text("h1") == "ChatGPT sign-in did not finish"
    opened.value.close()
    page.wait_for_function("document.querySelector('#chatgpt-status').textContent.includes('did not let this app use your plan')")
    assert page.inner_text("#chatgpt-sign-in") == "Sign in with ChatGPT again" and sign_in.revocations[-1]["token"] == "rt-1"
    # No one is signed in, but the sign-in registered the app for Ada: another account needs a registration
    # of its own, so the page offers it.
    assert page.is_visible("#chatgpt-another") and page.is_visible("#chatgpt-another-hint")
    # The next sign-in asks ChatGPT for its permission screen again, and the page says what to do there.
    waiting["on"] = True
    with context.expect_page() as opened:
        page.click("#chatgpt-sign-in")
    opened.value.wait_for_url("https://auth.openai.com/**")
    page.wait_for_selector("#chatgpt-cancel:not([hidden])")
    assert page.inner_text("#chatgpt-status").endswith("On ChatGPT's permission screen, allow Effect Size Finder to use your plan.")
    assert dict(httpx.URL(opened.value.url).params)["prompt"] == "consent"
    opened.value.close()
    page.click("#chatgpt-cancel")
    page.wait_for_selector("#chatgpt-cancel", state="hidden")
    assert page.inner_text("#chatgpt-status").endswith(
        "The next sign-in shows ChatGPT's permission screen again: allow Effect Size Finder to use your plan there.")
    waiting["on"] = False
    sign_in.scope = SCOPES
    with context.expect_page() as opened:
        page.click("#chatgpt-sign-in")
    opened.value.wait_for_url("**/auth/callback?**")
    page.wait_for_function("document.querySelector('#chatgpt-status').textContent.startsWith('Signed in as Ada Lovelace')")
    assert sign_in.authorizations[-1]["prompt"] == "consent" and "different ChatGPT account" not in page.inner_text("#chatgpt-status")
    opened.value.close()

    # Bob signs in on a registration of his own, and the page says the plan is now his.
    as_bob(sign_in)
    sign_in.token_changes = {"earliest_refresh_at": time.time() + 3600 + REFRESH_WAIT + 40}  # for the end of the test
    assert page.is_visible("#chatgpt-another")
    with context.expect_page() as opened:
        page.click("#chatgpt-another")
    opened.value.wait_for_url("**/auth/callback?**")
    page.wait_for_function("document.querySelector('#chatgpt-status').textContent.startsWith('Signed in as Bob Example')")
    assert "This is a different ChatGPT account from the one signed in before, so the searches now run on its plan." in page.inner_text("#chatgpt-status")
    registration = sign_in.authorizations[-1]
    assert registration["client_id"] == REGISTRATION_ENTRY and registration["agent_name_hint"] == APP_NAME and "login_hint" not in registration

    # An hour on, the token has run out and ChatGPT doesn't allow its renewal yet: the page says so.
    served["skew"][0] = 3600
    card(page, "anthropic").click()
    with page.expect_response("**/api/chatgpt?refresh=true"):
        card(page, "chatgpt").click()
    page.wait_for_function("document.querySelector('#chatgpt-status').textContent.includes('renew your sign-in yet')")
    assert page.inner_text("#chatgpt-status").startswith("Signed in as Bob Example")
    assert page.inner_text("#chatgpt-status").endswith(REFRESH_NOT_READY)
    assert not page.errors, page.errors
    context.close()


def test_signed_in_the_page_shows_the_controls_its_messages_name(browser, served):
    sign_in = served["sign_in"]
    context, page = open_page(browser, served)
    waiting = {"on": False}
    context.route("https://auth.openai.com/**", chatgpt_signs_in(sign_in, served["port"], waiting))
    page.wait_for_selector("#agents-page:not([hidden])")
    card(page, "chatgpt").click()
    with context.expect_page() as opened:
        page.click("#chatgpt-sign-in")
    opened.value.wait_for_url("**/auth/callback?**")
    opened.value.close()
    page.wait_for_function("document.querySelector('#chatgpt-status').textContent.startsWith('Signed in as Ada Lovelace')")
    # Signed in, with nothing to fix: no sign-in to make, and another account to switch to, with how.
    assert page.is_hidden("#chatgpt-sign-in") and page.is_visible("#chatgpt-another") and page.is_visible("#chatgpt-sign-out")
    assert page.inner_text("#chatgpt-another-hint") == ANOTHER_HINT and page.is_visible("#chatgpt-another-hint")

    # A switch whose ChatGPT tab never comes back leaves Ada signed in, and no message about it.
    waiting["on"] = True
    with context.expect_page() as opened:
        page.click("#chatgpt-another")
    opened.value.wait_for_url("https://auth.openai.com/**")
    page.wait_for_selector("#chatgpt-cancel:not([hidden])")
    served["skew"][0] = PENDING_SECONDS + 1
    page.wait_for_selector("#chatgpt-cancel", state="hidden")
    opened.value.close()
    status = page.inner_text("#chatgpt-status")
    assert status.startswith("Signed in as Ada Lovelace") and "didn't come back" not in status
    assert page.is_hidden("#chatgpt-sign-in") and page.is_visible("#chatgpt-another")
    assert httpx.get(served["url"] + "api/chatgpt").json()["error"] is None

    # A switch to Bob, whose plan ChatGPT withholds: Ada's sign-in serves on, and the sign-in the
    # message asks for is on screen.
    waiting["on"] = False
    as_bob(sign_in)
    sign_in.scope, sign_in.refresh = WITHOUT_PLAN, "bobs-refresh"
    with context.expect_page() as opened:
        page.click("#chatgpt-another")
    opened.value.wait_for_url("**/auth/callback?**")
    opened.value.close()
    page.wait_for_function("document.querySelector('#chatgpt-status').textContent.includes('did not let this app use your plan')")
    status = page.inner_text("#chatgpt-status")
    assert status.startswith("Signed in as Ada Lovelace") and "sign in with ChatGPT again" in status
    assert page.is_visible("#chatgpt-sign-in") and page.inner_text("#chatgpt-sign-in") == "Sign in with ChatGPT again"
    # It asks ChatGPT for its permission screen, where Bob allows the plan; Ada's grant is given back.
    sign_in.scope = SCOPES
    with context.expect_page() as opened:
        page.click("#chatgpt-sign-in")
    opened.value.wait_for_url("**/auth/callback?**")
    opened.value.close()
    page.wait_for_function("document.querySelector('#chatgpt-status').textContent.startsWith('Signed in as Bob Example')")
    assert sign_in.authorizations[-1]["prompt"] == "consent" and sign_in.authorizations[-1]["client_id"] == "app_Bob"
    assert sign_in.revocations[-1] == {"token": "rt-1", "token_type_hint": "refresh_token", "client_id": "app_Test123"}
    assert page.is_hidden("#chatgpt-sign-in") and "different ChatGPT account" in page.inner_text("#chatgpt-status")

    # Signed out, the app is still registered for Bob (until another account registers it anew), so switching
    # stays on offer.
    page.click("#chatgpt-sign-out")
    page.wait_for_function("!document.querySelector('#chatgpt-sign-in').hidden")
    assert page.is_visible("#chatgpt-another") and page.is_visible("#chatgpt-another-hint")
    assert not page.errors, page.errors
    context.close()
