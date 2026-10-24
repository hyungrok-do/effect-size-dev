"""The local edition's page (web/) against the server, driven in Chromium: signing in with ChatGPT
and running a search on OpenAI models with that plan. OpenAI's sign-in service and the Responses API
are the test fakes; the browser's trip to ChatGPT's sign-in page is answered with the redirect back
to the server's /auth/callback, as ChatGPT sends it."""

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
from test_chatgpt import FakeOpenAI as FakeSignIn

sync_api = pytest.importorskip("playwright.sync_api")
uvicorn = pytest.importorskip("uvicorn")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture()
def served(tmp_path):
    """The app on a loopback port, with OpenAI's sign-in and Responses API faked."""
    settings = Settings(data_dir=tmp_path, web_dir=REPO_ROOT / "web", contact_email="test@example.com", audit_fraction=0.0)
    sign_in = FakeSignIn()
    responses = FakeOpenAI()
    session = ChatGPTSession(tmp_path / "chatgpt.json", client=httpx.Client(transport=httpx.MockTransport(sign_in)))
    plans = []

    def on_plan(chatgpt):
        chosen = chatgpt.state()
        plans.append((chosen["manager_model"], chosen["worker_model"]))
        return OpenAILLM(settings, chatgpt.access_token, chosen["manager_model"], chosen["worker_model"],
                         client=httpx.Client(transport=httpx.MockTransport(responses)), reasoning_levels=chatgpt.reasoning_levels)

    app = create_app(settings, llm=FakeLLM(), sources=Sources(settings, client=mock_client(LiteratureAPIs())),
                     store=Store(tmp_path / "test.sqlite3"), chatgpt=session, llm_for_chatgpt=on_plan)
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    yield {"url": f"http://127.0.0.1:{port}/", "port": port, "sign_in": sign_in, "responses": responses, "plans": plans, "path": tmp_path}
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def browser():
    executable = os.environ.get("ESF_CHROMIUM") or next(iter(sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux/chrome"))[::-1]), None)
    with sync_api.sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=executable)
        yield b
        b.close()


def test_sign_in_with_chatgpt_and_search_on_the_plan(browser, served):
    sign_in, responses = served["sign_in"], served["responses"]
    context = browser.new_context(viewport={"width": 1280, "height": 900})

    def chatgpt_sign_in_page(route, request):
        # The person signs in; ChatGPT sends the browser back to the callback on 127.0.0.1.
        assert urlparse(request.url).hostname == "auth.openai.com"
        callback = f"http://127.0.0.1:{served['port']}/auth/callback?{sign_in.authorize(request.url)}"
        route.fulfill(status=302, headers={"location": callback})

    context.route("https://auth.openai.com/**", chatgpt_sign_in_page)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.goto(served["url"])
    assert page.is_visible("#anthropic-box") and page.is_hidden("#chatgpt-box")
    page.check("input[name='provider'][value='chatgpt']")
    assert page.is_hidden("#anthropic-box") and page.is_visible("#chatgpt-box")
    assert "Plus or Pro" in page.inner_text("#chatgpt-status")
    # Without a sign-in the page asks for one instead of starting.
    page.fill("#paragraph", "We propose a two-arm randomized trial of a 12-week mindfulness app versus waitlist for PHQ-9 in students.")
    page.click("#read")
    assert page.inner_text("#chatgpt-status") == "Sign in with ChatGPT first."

    with context.expect_page() as opened:
        page.click("#chatgpt-sign-in")
    tab = opened.value
    tab.wait_for_load_state()
    assert tab.inner_text("h1") == "Signed in with ChatGPT"
    assert tab.url.startswith(f"http://127.0.0.1:{served['port']}/auth/callback?code=")
    page.wait_for_function("document.querySelector('#chatgpt-status').textContent.startsWith('Signed in as')")
    assert page.inner_text("#chatgpt-status").startswith("Signed in as Ada Lovelace (ada@example.com).")
    authorization = sign_in.authorizations[0]
    assert authorization["redirect_uri"] == f"http://127.0.0.1:{served['port']}/auth/callback"
    assert authorization["agent_name_hint"] == "Effect Size Finder"
    # The page lists the plan's models; the default choice is OpenAI's lead model and its efficient one.
    options = page.locator("#chatgpt-worker option").all_inner_texts()
    assert options == ["GPT-6.1 Sol", "GPT-6 Luna", "GPT-6 Mini"], options
    assert page.input_value("#chatgpt-manager") == "gpt-6.1-sol" and page.input_value("#chatgpt-worker") == "gpt-6-luna"
    page.select_option("#chatgpt-worker", "gpt-6-mini")
    page.wait_for_function("document.querySelector('#models').textContent.includes('GPT-6 Mini screens')")
    assert page.inner_text("#models").startswith("GPT-6.1 Sol plans and reviews · GPT-6 Mini screens and reads")
    # Only the registration and the choice of models are kept on disk; the tokens stay in memory.
    saved = (served["path"] / "chatgpt.json").read_text()
    assert "app_Test123" in saved and "gpt-6-mini" in saved and "at-1" not in saved and "rt-1" not in saved
    assert "sent to OpenAI's API" in page.inner_text(".notice")

    page.click("#read")
    page.wait_for_selector("#spec:not([hidden])")
    assert "How GPT-6.1 Sol read your paragraph" in page.inner_text("#spec .summary")
    page.click("#run")
    page.wait_for_selector("#map-body .map-stage[data-stage='ready'][data-state='done']", timeout=30000)
    page.wait_for_selector("#review:not([hidden]) .paper")
    assert served["plans"] and set(served["plans"]) == {("gpt-6.1-sol", "gpt-6-mini")}
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

    page.click("#chatgpt-sign-out")
    page.wait_for_function("!document.querySelector('#chatgpt-sign-in').hidden")
    assert sign_in.revocations and sign_in.revocations[-1]["token"] == "rt-1"
    page.click("#summarize")
    assert page.inner_text("#chatgpt-status") == "Sign in with ChatGPT first."
    assert not errors, errors


def test_a_job_on_the_plan_needs_a_sign_in(served):
    response = httpx.post(served["url"] + "api/projects", json={"paragraph": "A randomized trial of a mindfulness app for PHQ-9."},
                          headers={"X-Model-Provider": "chatgpt"})
    assert response.status_code == 409 and response.json()["detail"] == "Sign in with ChatGPT first, in Setup."
    assert httpx.post(served["url"] + "api/projects", json={"paragraph": "A randomized trial of a mindfulness app for PHQ-9."},
                      headers={"X-Model-Provider": "gemini"}).status_code == 400
    state = httpx.get(served["url"] + "api/chatgpt").json()
    assert state["status"] == "disconnected" and state["remote"] is False and "token" not in str(state).lower()
    # A callback that does not belong to a sign-in this server started changes nothing.
    page = httpx.get(served["url"] + "auth/callback?code=abc&state=forged")
    assert page.status_code == 400 and "did not finish" in page.text and page.headers["cache-control"] == "no-store"
    assert httpx.get(served["url"] + "api/chatgpt").json()["status"] == "disconnected"
