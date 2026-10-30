import csv
import io
import json
import logging
import os
import re
import socket
import stat
import threading
import time

import anthropic
import httpx
import httpx2
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.requests import Request

from esf import cli
from esf.chatgpt import (
    APP_NAME, JOB_REFRESH_WAIT, NO_CALLBACK, PENDING_SECONDS, REFRESH_NOT_READY, REFRESH_WAIT, REGISTRATION_ENTRY, SCOPES,
)
from esf.config import MODEL_CHOICES, REPO_ROOT, Settings
from esf.llm import NO_KEY, ClaudeLLM
from esf.openai_llm import OpenAILLM
from esf.server import EXPORT_COLUMNS, _remote, create_app, csv_cell, plan_llm
from esf.sources import Sources
from esf.store import Store
from fixtures import SPEC, FakeLLM, LiteratureAPIs, mock_client, streamed
from test_chatgpt import HOST, PORT, WITHOUT_PLAN, as_bob, new_session, signed_in
from test_llm import profile_on_disk

PARAGRAPH = "A randomized trial of a mindfulness app for PHQ-9."
BASE = "http://127.0.0.1:8000"  # the server answers only to its address or localhost


LOCAL = ("127.0.0.1", 50000)  # a browser on the computer that runs the server


def make_client(tmp_path, llm=None, llm_for_key=None, llm_for_openai=None, chatgpt=None, llm_for_chatgpt=None,
                access_token=None, default_llm=False, **settings):
    """The app as this computer's browser reaches it. `default_llm` leaves the app to build its own Claude
    client, from the server's credential."""
    settings = Settings(data_dir=tmp_path, web_dir=REPO_ROOT / "web", contact_email="test@example.com", audit_fraction=0.0, **settings)
    app = create_app(
        settings,
        llm=None if default_llm else llm or FakeLLM(),
        sources=Sources(settings, client=mock_client(LiteratureAPIs())),
        store=Store(tmp_path / "test.sqlite3"),
        llm_for_key=llm_for_key,
        llm_for_openai=llm_for_openai,
        chatgpt=chatgpt,
        llm_for_chatgpt=llm_for_chatgpt,
        access_token=access_token,
    )
    return TestClient(app, base_url=BASE, client=LOCAL)


def wait_for(client, project_id, statuses, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        project = client.get(f"/api/projects/{project_id}").json()
        if project["status"] in statuses:
            return project
        time.sleep(0.05)
    raise AssertionError(f"still {project['status']}")


def test_full_flow_over_http(tmp_path):
    client = make_client(tmp_path)
    assert client.get("/").status_code == 200
    assert client.get("/api/config").json()["worker_model"] == "claude-haiku-5-5"

    assert client.post("/api/projects", json={"paragraph": "too short"}).status_code == 422
    project_id = client.post("/api/projects", json={"paragraph": "A randomized trial of a mindfulness app for PHQ-9."}).json()["id"]
    project = wait_for(client, project_id, {"spec_ready"})
    assert project["spec"]["outcomes"][0]["measure"] == "PHQ-9"

    edited = SPEC.model_dump()
    edited["outcomes"][0]["timepoint"] = "8 to 12 weeks"
    assert client.put(f"/api/projects/{project_id}/spec", json=edited).status_code == 200
    assert client.post(f"/api/projects/{project_id}/run").status_code == 202
    project = wait_for(client, project_id, {"done", "error"})
    assert project["status"] == "done", project["error"]
    assert project["spec"]["outcomes"][0]["timepoint"] == "8 to 12 weeks"
    assert project["counts"] == {"extracted": 8, "screened_out": 1}
    assert [e["level"] for e in project["search_log"]] == ["reviews", "primary", "full_text", "registry", "cited"]
    assert project["level_names"]["cited"] == "Cited by the included reviews"

    papers = {p["ext_id"]: p for p in client.get(f"/api/projects/{project_id}/papers").json()}
    trial = papers["11111111"]
    assert trial["links"]["PMC"].endswith("/PMC9000001/") and "NCT01234567" in trial["links"]

    url = f"/api/projects/{project_id}/papers/{trial['id']}/decisions"
    assert client.put(url, json={"result_index": 0, "decision": "include", "reason": ""}).status_code == 200
    assert client.put(url, json={"result_index": 5, "decision": "include"}).status_code == 400
    papers = {p["ext_id"]: p for p in client.get(f"/api/projects/{project_id}/papers").json()}
    assert papers["11111111"]["decisions"]["0"]["decision"] == "include"

    # The manager's reading of each paper comes to the page with it, and goes into the exports.
    assert trial["context"]["status"] == "done" and trial["context"]["checks"] == {"sample_size": None, "results": ["exact"]}
    assert papers["33333333"]["context"] is None
    export = client.get(f"/api/projects/{project_id}/export.csv?included_only=true").text
    rows = list(csv.DictReader(io.StringIO(export)))
    assert {r["id"] for r in rows} == {"11111111"} and len(rows) == 6
    assert export.splitlines()[0].split(",") == EXPORT_COLUMNS
    assert {(r["design"], r["estimand_population"], r["model"], r["vs_plan"], r["differences"]) for r in rows} == {
        ("two-arm parallel RCT, individually randomized", "all randomized (ITT)", "ANCOVA adjusted for baseline", "same", "")}
    exported = client.get(f"/api/projects/{project_id}/export.json").json()
    assert exported["project"]["id"] == project_id
    assert next(p for p in exported["papers"] if p["id"] == trial["id"])["context"] == trial["context"]

    source = client.get(f"/api/projects/{project_id}/papers/{trial['id']}/source")
    assert source.status_code == 200 and "8.1 (4.2)" in source.text

    # Re-extracting a screened-out record on request.
    excluded = papers["33333333"]
    assert client.post(f"/api/projects/{project_id}/papers/{excluded['id']}/extract").status_code == 202
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        status = next(p for p in client.get(f"/api/projects/{project_id}/papers").json() if p["id"] == excluded["id"])["status"]
        if status not in ("fetching", "extracting"):
            break
        time.sleep(0.05)
    assert status == "extracted"


def test_a_key_from_the_page_is_used_for_that_job(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    default, keyed, keys = FakeLLM(), FakeLLM(), []

    def llm_for_key(key, manager, worker):
        keys.append((key, manager, worker))
        return keyed

    client = make_client(tmp_path, llm=default, llm_for_key=llm_for_key)
    assert client.get("/api/config").json()["server_has_key"] is False

    # An OpenAI key sent beside it is not Anthropic's to see.
    headers = {"X-Anthropic-Api-Key": "  sk-ant-test-123  ", "X-OpenAI-Api-Key": "sk-proj-not-for-anthropic"}
    project_id = client.post("/api/projects", json={"paragraph": PARAGRAPH}, headers=headers).json()["id"]
    wait_for(client, project_id, {"spec_ready"})
    assert keys == [("sk-ant-test-123", "claude-opus-5-5", "claude-haiku-5-5")] and keyed.calls == ["spec"] and default.calls == []

    # The models the page chose for the roles.
    chosen = {**headers, "X-Model-Provider": "anthropic", "X-Manager-Model": "claude-sonnet-5-5", "X-Worker-Model": "claude-sonnet-5-5"}
    client.post(f"/api/projects/{project_id}/run", headers=chosen)
    wait_for(client, project_id, {"done"})
    assert "search" in keyed.calls and default.calls == []
    assert keys[-1] == ("sk-ant-test-123", "claude-sonnet-5-5", "claude-sonnet-5-5")

    # Without a key the server falls back to its own credentials.
    other = client.post("/api/projects", json={"paragraph": "Another randomized trial description here."}).json()["id"]
    wait_for(client, other, {"spec_ready"})
    assert default.calls == ["spec"]

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-server")
    assert client.get("/api/config").json()["server_has_key"] is True


def test_without_a_key_the_server_runs_the_chosen_claude_models(tmp_path):
    requests = []

    def anthropic_api(request):
        body = json.loads(request.content)
        requests.append((request, body))
        return httpx2.Response(200, content=streamed(body["model"], SPEC.model_dump()), headers={"content-type": "text/event-stream"})

    own = anthropic.Anthropic(api_key="sk-ant-server", max_retries=0,
                              http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(anthropic_api)))
    client = make_client(tmp_path, llm=ClaudeLLM(Settings(), own))
    for manager in ("claude-haiku-5-5", "claude-opus-5-5"):
        project_id = client.post("/api/projects", json={"paragraph": PARAGRAPH}, headers={"X-Manager-Model": manager}).json()["id"]
        wait_for(client, project_id, {"spec_ready"})
    (haiku_request, haiku), (opus_request, opus) = requests
    # The server-side fallback goes with the model, not the role: Haiku as the manager has none.
    assert haiku["model"] == "claude-haiku-5-5" and "fallbacks" not in haiku and "anthropic-beta" not in haiku_request.headers
    assert opus["model"] == "claude-opus-5-5" and opus["fallbacks"] == "default"
    assert all("sk-ant-server" in request.headers.get("x-api-key", "") + request.headers.get("authorization", "") for request, _ in requests)


def test_openai_runs_with_a_key_from_the_page_or_the_server(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    default, on_openai, made, claude = FakeLLM(), FakeLLM(), [], []

    def llm_for_openai(key, manager, worker):
        made.append((key, manager, worker))
        return on_openai

    client = make_client(tmp_path, llm=default, llm_for_key=lambda *args: claude.append(args) or FakeLLM(), llm_for_openai=llm_for_openai)
    config = client.get("/api/config").json()
    assert config["server_has_openai_key"] is False and config["model_choices"] == MODEL_CHOICES

    # No OpenAI key from the page or the server: nothing starts. An Anthropic key is not OpenAI's to see.
    response = client.post("/api/projects", json={"paragraph": PARAGRAPH}, headers={"X-Model-Provider": "openai", "X-Anthropic-Api-Key": "sk-ant-test-123"})
    assert response.status_code == 409 and response.json()["detail"] == "Enter your OpenAI API key first, on the Agents page."
    assert client.get("/api/projects").json() == []

    headers = {"X-Model-Provider": "openai", "X-OpenAI-Api-Key": " sk-proj-test ", "X-Anthropic-Api-Key": "sk-ant-test-123"}
    project_id = client.post("/api/projects", json={"paragraph": PARAGRAPH}, headers=headers).json()["id"]
    wait_for(client, project_id, {"spec_ready"})
    assert made == [("sk-proj-test", "gpt-6.1-sol", "gpt-6-luna")] and on_openai.calls == ["spec"]
    assert default.calls == [] and claude == []

    client.post(f"/api/projects/{project_id}/run", headers={**headers, "X-Manager-Model": "gpt-6-luna", "X-Worker-Model": "gpt-6-luna"})
    wait_for(client, project_id, {"done"})
    assert made[-1] == ("sk-proj-test", "gpt-6-luna", "gpt-6-luna") and "search" in on_openai.calls

    # The server's own key when the page sends none.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-server-key")
    assert client.get("/api/config").json()["server_has_openai_key"] is True
    assert client.post(f"/api/projects/{project_id}/summary", headers={"X-Model-Provider": "openai"}).status_code == 202
    assert made[-1] == ("sk-server-key", "gpt-6.1-sol", "gpt-6-luna")
    deadline = time.monotonic() + 10
    while client.get(f"/api/projects/{project_id}").json()["summary"]["status"] == "running" and time.monotonic() < deadline:
        time.sleep(0.05)
    assert on_openai.calls[-1] == "summarize" and default.calls == [] and claude == []


def test_a_job_on_an_unknown_provider_or_model_is_refused(tmp_path):
    client = make_client(tmp_path)

    def post(headers):
        return client.post("/api/projects", json={"paragraph": PARAGRAPH}, headers=headers)

    assert post({"X-Model-Provider": "gemini"}).status_code == 400
    for bad in ("claude opus", "-claude-opus-5-5", "claude-opus-5-5;x", "claude/opus", "x" * 101):
        response = post({"X-Manager-Model": bad})
        assert response.status_code == 400 and response.json()["detail"] == "The manager model's name is not valid.", bad
    response = post({"X-Model-Provider": "openai", "X-OpenAI-Api-Key": "sk-proj-test", "X-Worker-Model": "gpt 6"})
    assert response.status_code == 400 and response.json()["detail"] == "The worker model's name is not valid."
    # A ChatGPT plan runs the models chosen for the sign-in: the model headers are not read.
    assert post({"X-Model-Provider": "chatgpt", "X-Manager-Model": "not valid"}).status_code == 409
    assert client.get("/api/projects").json() == []


def test_only_the_servers_own_pages_may_reach_it(tmp_path):
    client = make_client(tmp_path)
    # A page that rebinds its own name to this computer is refused; the server's addresses and localhost are not.
    response = client.get("/api/config", headers={"Host": "rebind.attacker.example:8000"})
    assert response.status_code == 403 and "localhost" in response.json()["detail"]
    for host in ("127.0.0.1:8000", "localhost:8000", "LOCALHOST", "[::1]:8000", "192.168.1.20"):
        assert client.get("/api/config", headers={"Host": host}).status_code == 200, host
    # A change must come from the server's own page: another site's form post is refused.
    for headers in ({"Origin": "https://evil.example"}, {"Origin": "null"}, {"Origin": "http://127.0.0.1:3000"},
                    {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"}):
        response = client.post("/api/chatgpt/sign-out", headers=headers)
        assert response.status_code == 403 and response.json()["detail"] == "Only this server's own page may make changes.", headers
    assert client.post("/api/chatgpt/sign-out", headers={"Origin": BASE, "Sec-Fetch-Site": "same-origin"}).status_code == 200
    # Behind a proxy the page's Origin is the proxy's name: the browser's Sec-Fetch-Site still says it is the same page.
    assert client.post("/api/chatgpt/sign-out", headers={"Origin": "https://esf.example.org", "Sec-Fetch-Site": "same-origin"}).status_code == 200
    # Without Sec-Fetch-Site, the Origin must be the Host the request came to.
    assert client.post("/api/chatgpt/sign-out", headers={"Host": "localhost:8000", "Origin": "http://localhost:8000"}).status_code == 200
    assert client.post("/api/chatgpt/sign-out", headers={"Host": "127.0.0.1:8000", "Origin": "http://localhost:8000"}).status_code == 403
    # No other site may frame the page and have the person click in it.
    for path in ("/", "/api/config"):
        response = client.get(path)
        assert response.headers["x-frame-options"] == "DENY" and response.headers["content-security-policy"] == "frame-ancestors 'none'", path
    # ChatGPT sends the browser back to the callback from its own site: reading stays open.
    assert client.get("/auth/callback?code=abc&state=forged", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 400
    # A name of its own, when the server is told to answer to it.
    named = make_client(tmp_path / "named", allowed_hosts=("esf.example.org",))
    assert named.get("/api/config", headers={"Host": "esf.example.org"}).status_code == 200


def test_the_chatgpt_sign_in_is_only_for_this_computer(tmp_path):
    session, api, _ = signed_in(tmp_path)
    plans = []
    local = make_client(tmp_path, chatgpt=session, llm_for_chatgpt=lambda chatgpt: plans.append(chatgpt) or FakeLLM())
    remote = TestClient(local.app, base_url=BASE, client=("192.0.2.2", 50000))
    # Another computer learns only whether someone is signed in, and may not use, change or end the sign-in.
    assert remote.get("/api/chatgpt?refresh=true").json() == {"status": "connected", "remote": True}
    for response in (remote.post("/api/chatgpt/sign-out"), remote.post("/api/chatgpt/cancel"),
                     remote.put("/api/chatgpt/models", json={"manager_model": "gpt-6-luna", "worker_model": "gpt-6-luna"})):
        assert response.status_code == 403 and "computer that runs the server" in response.json()["detail"]
    assert remote.post("/api/chatgpt/sign-in").status_code == 400
    response = remote.post("/api/projects", json={"paragraph": PARAGRAPH}, headers={"X-Model-Provider": "chatgpt"})
    assert response.status_code == 403 and response.json()["detail"] == "Your ChatGPT plan can be used only from the computer that runs the server."
    assert session.state()["status"] == "connected" and session.state()["manager_model"] == "gpt-6.1-sol" and plans == []
    # Behind a proxy every visitor comes from 127.0.0.1: a forwarded request, or one to another name, is remote too.
    proxied = TestClient(local.app, base_url=BASE, client=LOCAL)
    assert proxied.get("/api/chatgpt").json()["remote"] is False
    for headers in ({"X-Forwarded-For": "203.0.113.9"}, {"Forwarded": "for=203.0.113.9"}, {"X-Real-IP": "203.0.113.9"},
                    {"Host": "192.168.1.20:8000"}):
        assert proxied.get("/api/chatgpt", headers=headers).json() == {"status": "connected", "remote": True}, headers
        assert proxied.post("/api/chatgpt/sign-out", headers=headers).status_code == 403, headers
    assert session.state()["status"] == "connected"

    # This computer sees who is signed in and runs jobs on the plan.
    state = local.get("/api/chatgpt").json()
    assert state["email"] == "ada@example.com" and state["remote"] is False
    project_id = local.post("/api/projects", json={"paragraph": PARAGRAPH}, headers={"X-Model-Provider": "chatgpt"}).json()["id"]
    wait_for(local, project_id, {"spec_ready"})
    assert plans == [session]

    # A model the plan does not offer would fail every call: the job does not start.
    api.catalog = {"models": [{"slug": "gpt-7", "display_name": "GPT-7", "visibility": "list"}]}
    local.get("/api/chatgpt?refresh=true")
    response = local.post(f"/api/projects/{project_id}/run", headers={"X-Model-Provider": "chatgpt"})
    assert response.status_code == 409
    assert response.json()["detail"] == "Your ChatGPT plan does not offer gpt-6.1-sol. Choose the manager model on the Agents page."
    # Choosing one role keeps the other as it was, though the plan doesn't list it.
    assert local.put("/api/chatgpt/models", json={"manager_model": "gpt-7", "worker_model": "gpt-6-luna"}).status_code == 200
    response = local.post(f"/api/projects/{project_id}/run", headers={"X-Model-Provider": "chatgpt"})
    assert response.status_code == 409 and "Choose the workers' model" in response.json()["detail"]
    assert local.post("/api/chatgpt/cancel").json()["pending"] is False


def test_sign_in_errors_come_back_with_their_message(tmp_path):
    session, api, _ = new_session(tmp_path)
    api.offline = True
    client = make_client(tmp_path, chatgpt=session)
    response = client.post("/api/chatgpt/sign-in")
    assert response.status_code == 503 and "sign-in service could not be reached" in response.json()["detail"]

    session, api, _ = signed_in(tmp_path / "saved")
    client = make_client(tmp_path / "saved", chatgpt=session)
    (tmp_path / "blocked").write_text("a file where the settings folder would be")
    session.path = tmp_path / "blocked" / "chatgpt.json"
    response = client.put("/api/chatgpt/models", json={"manager_model": "gpt-6-luna", "worker_model": "gpt-6-mini"})
    assert response.status_code == 500 and "Check that the folder is writable" in response.json()["detail"]
    assert (session.state()["manager_model"], session.state()["worker_model"]) == ("gpt-6.1-sol", "gpt-6-luna")


def test_the_model_choices_are_the_pages():
    # The browser edition's list (site/lib/providers.js), which both pages offer.
    text = (REPO_ROOT / "site" / "lib" / "providers.js").read_text()
    block = re.search(r"export const MODEL_CHOICES = \{(.*?)\n\};", text, re.S).group(1)
    pages = {name: re.findall(r"'([^']+)'", models) for name, models in re.findall(r"(\w+): \[([^\]]*)\]", block)}
    assert pages == MODEL_CHOICES


def test_a_missing_key_gives_an_actionable_error(tmp_path):
    llm = FakeLLM()

    def no_credentials(paragraph, sink=None):
        raise TypeError("Could not resolve authentication method. Expected one of api_key, auth_token, or credentials to be set.")

    llm.read_spec = no_credentials
    client = make_client(tmp_path, llm=llm)
    project_id = client.post("/api/projects", json={"paragraph": "A randomized trial of a mindfulness app for PHQ-9."}).json()["id"]
    project = wait_for(client, project_id, {"error"})
    assert "Enter a valid key in the API key box" in project["error"]


def wait_for_context(client, project_id, paper_id, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        context = next(p for p in client.get(f"/api/projects/{project_id}/papers").json() if p["id"] == paper_id)["context"]
        if (context or {}).get("status") != "running":
            return context
        time.sleep(0.05)
    raise AssertionError("still reading")


def test_the_manager_reads_the_context_of_a_paper_already_extracted(tmp_path):
    llm, keys = FakeLLM(), []
    client = make_client(tmp_path, llm=FakeLLM(), llm_for_key=lambda key, manager, worker: keys.append((key, manager)) or llm)
    project_id = client.post("/api/projects", json={"paragraph": PARAGRAPH}).json()["id"]
    wait_for(client, project_id, {"spec_ready"})
    client.post(f"/api/projects/{project_id}/run")
    wait_for(client, project_id, {"done"})
    papers = {p["ext_id"]: p for p in client.get(f"/api/projects/{project_id}/papers").json()}
    trial, excluded = papers["11111111"], papers["33333333"]
    # As a paper extracted before the manager read contexts: read on request, on the models the page chose.
    store = client.app.state.store
    store.update_paper(trial["id"], context=None)
    url = f"/api/projects/{project_id}/papers/{trial['id']}/context"
    headers = {"X-Anthropic-Api-Key": "sk-ant-test", "X-Manager-Model": "claude-sonnet-5-5"}
    assert client.post(url, headers=headers).status_code == 202
    context = wait_for_context(client, project_id, trial["id"])
    assert context["status"] == "done" and context["checks"] == {"sample_size": None, "results": ["exact"]}
    assert context["result"]["results"][0]["quote"] == "Analyses followed the intention-to-treat principle."
    assert llm.calls == ["context"] and keys == [("sk-ant-test", "claude-sonnet-5-5")]
    # Read again after a failure.
    store.update_paper(trial["id"], context={"status": "error", "error": "overloaded"})
    assert client.post(url, headers=headers).status_code == 202
    assert wait_for_context(client, project_id, trial["id"])["status"] == "done" and llm.calls == ["context", "context"]

    # Not while it is being read, nor for a paper with nothing to read, nor during a run.
    store.update_paper(trial["id"], context={"status": "running"})
    assert client.post(url).status_code == 409
    store.update_paper(trial["id"], context=None)
    response = client.post(f"/api/projects/{project_id}/papers/{excluded['id']}/context")
    assert response.status_code == 400 and response.json()["detail"] == "Extract this paper first."
    store.update_paper(trial["id"], extraction={**trial["extraction"], "results": []})
    assert client.post(url).status_code == 400
    store.update_paper(trial["id"], extraction=trial["extraction"])
    store.update_project(project_id, status="extracting")
    assert client.post(url).status_code == 409
    store.update_project(project_id, status="done")
    assert client.post(f"/api/projects/{project_id}/papers/999999/context").status_code == 404
    assert client.post(f"/api/projects/nope/papers/{trial['id']}/context").status_code == 404
    assert llm.calls == ["context", "context"]
    # Extracting it again clears the reading until the manager has read the new extraction.
    assert client.post(f"/api/projects/{project_id}/papers/{trial['id']}/extract", headers=headers).status_code == 202
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        paper = next(p for p in client.get(f"/api/projects/{project_id}/papers").json() if p["id"] == trial["id"])
        if paper["status"] == "extracted" and (paper["context"] or {}).get("status") != "running":
            break
        time.sleep(0.05)
    assert paper["context"]["status"] == "done" and llm.calls[-2:] == ["extract", "context"]


def test_extracting_a_paper_while_the_manager_reads_it_changes_nothing(tmp_path):
    """A paper being read holds its job: a request to extract it again is refused before its decisions
    or its status change, and the reading ends as it would have."""
    llm, reading, release = FakeLLM(), threading.Event(), threading.Event()
    client = make_client(tmp_path, llm=llm)
    project_id = client.post("/api/projects", json={"paragraph": PARAGRAPH}).json()["id"]
    wait_for(client, project_id, {"spec_ready"})
    client.post(f"/api/projects/{project_id}/run")
    wait_for(client, project_id, {"done"})
    trial = next(p for p in client.get(f"/api/projects/{project_id}/papers").json() if p["ext_id"] == "11111111")
    assert client.put(f"/api/projects/{project_id}/papers/{trial['id']}/decisions",
                      json={"result_index": 0, "decision": "include", "reason": ""}).status_code == 200
    study_context = llm.study_context

    def held(*args, **kwargs):
        reading.set()
        assert release.wait(10)
        return study_context(*args, **kwargs)

    llm.study_context = held
    client.app.state.store.update_paper(trial["id"], context=None)
    assert client.post(f"/api/projects/{project_id}/papers/{trial['id']}/context").status_code == 202
    assert reading.wait(10)
    response = client.post(f"/api/projects/{project_id}/papers/{trial['id']}/extract")
    assert response.status_code == 409
    release.set()
    context = wait_for_context(client, project_id, trial["id"])
    paper = next(p for p in client.get(f"/api/projects/{project_id}/papers").json() if p["id"] == trial["id"])
    assert context["status"] == "done" and paper["status"] == "extracted"
    assert paper["decisions"]["0"]["decision"] == "include" and paper["extraction"] == trial["extraction"]


def test_unknown_ids(tmp_path):
    client = make_client(tmp_path)
    assert client.get("/api/projects/nope").status_code == 404
    assert client.get("/api/projects/nope/papers").status_code == 404


def test_the_summary_over_http(tmp_path):
    llm, keys = FakeLLM(), []
    client = make_client(tmp_path, llm=FakeLLM(), llm_for_key=lambda key, manager, worker: keys.append(key) or llm)
    project_id = client.post("/api/projects", json={"paragraph": "A randomized trial of a mindfulness app for PHQ-9."}).json()["id"]
    wait_for(client, project_id, {"spec_ready"})
    client.post(f"/api/projects/{project_id}/run", headers={"X-Anthropic-Api-Key": "sk-ant-test"})
    project = wait_for(client, project_id, {"done"})
    # The overlap check runs after extraction, and the page is told what it found.
    assert {g["relation"] for g in project["overlap"]["report"]["groups"]} == {"same_study", "review_includes_study"}
    assert project["summary"] is None

    assert client.post(f"/api/projects/{project_id}/summary", headers={"X-Anthropic-Api-Key": "sk-ant-test"}).status_code == 202
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        summary = client.get(f"/api/projects/{project_id}").json()["summary"]
        if summary["status"] != "running":
            break
        time.sleep(0.05)
    assert summary["status"] == "done", summary
    assert summary["result"]["needs"][0]["values"] and summary["evidence"]["records"]
    assert keys == ["sk-ant-test", "sk-ant-test"] and llm.calls[-1] == "summarize"
    assert llm.calls.count("overlap") == 1  # the check from the run is still current
    assert client.get(f"/api/projects/{project_id}/export.json").json()["project"]["summary"]["status"] == "done"

    assert client.post("/api/projects/nope/summary").status_code == 404
    # A new search waits for a summary being written: it would replace the papers it is about.
    app_store = client.app.state.store
    app_store.update_project(project_id, summary={"status": "running", "previous": summary})
    assert client.post(f"/api/projects/{project_id}/run").status_code == 409
    app_store.update_project(project_id, summary=summary)
    # A paper's authors with their affiliations.
    trial = next(p for p in client.get(f"/api/projects/{project_id}/papers").json() if p["ext_id"] == "11111111")
    authors = client.get(f"/api/projects/{project_id}/papers/{trial['id']}/authors").json()["authors"]
    assert authors[0]["name"] == "Jisoo Kim" and "Seoul National University" in authors[0]["affiliations"][0]
    # The page's shared modules are served next to it.
    assert client.get("/lib/review-view.js").status_code == 200 and client.get("/lib/evidence.js").status_code == 200


# -- the access key ------------------------------------------------------------------------

ACCESS_TOKEN = "test-access-key-0123456789"
NEEDS_KEY = {"detail": "Open the address that esf serve printed in the terminal; it carries this server's access key.", "code": "access_key"}


def test_without_the_access_key_no_api_route_answers(tmp_path):
    session, api, _ = new_session(tmp_path / "plan")
    client = make_client(tmp_path, chatgpt=session, access_token=ACCESS_TOKEN)
    routes = [(method, route.path) for route in client.app.routes if isinstance(route, APIRoute) for method in route.methods]
    assert len([path for _, path in routes if path.startswith("/api/")]) >= 18
    for method, path in routes:
        if not path.startswith("/api/"):
            continue
        path = path.replace("{project_id}", "p1").replace("{paper_id}", "1")
        for headers in ({}, {"X-ESF-Token": "wrong"}, {"X-ESF-Token": ACCESS_TOKEN[:-1]}, {"X-ESF-Token": ACCESS_TOKEN + "x"}):
            response = client.request(method, path, headers=headers)
            assert (response.status_code, response.json()) == (401, NEEDS_KEY), (method, path, headers)
    assert client.get("/api/not-a-route").status_code == 401  # nothing under /api is described without it
    # Served under a prefix (a proxy's root_path), the routes still match /api/...: so does the check.
    prefixed = TestClient(client.app, base_url=BASE, client=LOCAL, root_path="/esf")
    assert prefixed.get("/esf/api/projects").status_code == 401 and prefixed.get("/api/projects").status_code == 401
    assert prefixed.get("/esf/api/projects", headers={"X-ESF-Token": ACCESS_TOKEN}).json() == []
    assert api.requests == [] and session.state()["pending"] is False  # no sign-in was started
    # The Host check still comes first, so a page that rebinds its name learns nothing either.
    assert client.get("/api/config", headers={"Host": "rebind.attacker.example"}).status_code == 403

    # The page and its modules hold no data: they load without the key, which the page then sends.
    for path in ("/", "/app.js", "/lib/review-view.js"):
        assert client.get(path).status_code == 200, path
    keyed = TestClient(client.app, base_url=BASE, client=LOCAL, headers={"X-ESF-Token": ACCESS_TOKEN})
    assert keyed.get("/api/config").status_code == 200 and keyed.get("/api/projects").json() == []
    project_id = keyed.post("/api/projects", json={"paragraph": PARAGRAPH}).json()["id"]
    assert wait_for(keyed, project_id, {"spec_ready"})["spec"]
    # ChatGPT's return to /auth/callback carries no key, and needs none: its state ties it to the sign-in.
    url = keyed.post("/api/chatgpt/sign-in").json()["url"]
    page = client.get(f"/auth/callback?{api.authorize(url)}")
    assert page.status_code == 200 and "Signed in with ChatGPT" in page.text
    assert keyed.get("/api/chatgpt").json()["email"] == "ada@example.com"


def test_esf_serve_prints_the_address_with_its_access_key_and_opens_it(tmp_path, monkeypatch, capsys):
    import uvicorn

    monkeypatch.setenv("ESF_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("ESF_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(cli, "to_terminal", lambda: True)  # printed to a terminal (see the test after this one)
    masks, opened, served = [], [], []
    monkeypatch.setattr(os, "umask", lambda mask: masks.append(mask) or 0o022)
    launcher = tmp_path / "data" / "open.html"
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: opened.append((url, launcher.read_text())) or True)
    monkeypatch.setattr(logging.getLogger("uvicorn.access"), "filters", [])
    announced = threading.Event()
    announce = cli.announce
    monkeypatch.setattr(cli, "announce", lambda *args, **kwargs: (announce(*args, **kwargs), announced.set()))

    def run(server, sockets=None):  # instead of serving: up at once, until the address is out
        server.started = True
        assert announced.wait(5)
        served.append(server.config)

    monkeypatch.setattr(uvicorn.Server, "run", run)
    cli.main(["serve", "--host", "0.0.0.0", "--port", "8123"])
    line = capsys.readouterr().out.strip().splitlines()[-1]
    token = line.rsplit("#key=", 1)[-1]
    assert line == f"Effect Size Finder is running. Open this address, which carries its access key: http://127.0.0.1:8123/#key={token}"
    assert len(token) >= 32 and masks == [0o077] and (served[0].host, served[0].port) == ("0.0.0.0", 8123)
    # The browser gets a page of the server's that sends it on: the key stays off the browser's command line.
    [(url, page)] = opened
    assert url == launcher.resolve().as_uri() and f'content="0;url=http://127.0.0.1:8123/#key={token}"' in page
    assert not launcher.exists()  # removed when the server stops
    # The app wants the key it printed.
    app = TestClient(served[0].app, base_url=BASE, client=LOCAL)
    assert app.get("/api/config").status_code == 401
    assert app.get("/api/config", headers={"X-ESF-Token": token}).status_code == 200
    assert [type(f) for f in logging.getLogger("uvicorn.access").filters] == [cli.HideCallbackQuery]

    # ESF_ACCESS_TOKEN keeps one key from launch to launch; --no-browser only prints the address.
    announced.clear()
    monkeypatch.setenv("ESF_ACCESS_TOKEN", "my-own.key_0123~")
    cli.main(["serve", "--no-browser", "--host", "::1"])
    assert capsys.readouterr().out.strip().endswith(": http://[::1]:8000/#key=my-own.key_0123~") and len(opened) == 1
    # Nor does it open one on Linux with no display (over SSH), where only a text browser could start.
    monkeypatch.setattr(cli.sys, "platform", "linux")
    for variable in ("DISPLAY", "WAYLAND_DISPLAY"):
        monkeypatch.delenv(variable, raising=False)
    announced.clear()
    cli.main(["serve"])
    assert "#key=my-own.key_0123~" in capsys.readouterr().out and len(opened) == 1
    monkeypatch.setenv("ESF_ACCESS_TOKEN", "not a key")
    with pytest.raises(SystemExit) as caught:
        cli.main(["serve"])
    assert "ESF_ACCESS_TOKEN may hold only" in str(caught.value)
    # ChatGPT's sign-in comes back to 127.0.0.1: the key is kept for that origin, not localhost's.
    assert cli.page_address("localhost", 9000, "abc") == "http://127.0.0.1:9000/#key=abc"
    assert cli.page_address("LocalHost", 9000, None) == "http://127.0.0.1:9000/"
    assert cli.page_address("192.168.1.20", 9000, "abc") == "http://192.168.1.20:9000/#key=abc"


@pytest.mark.skipif(os.name != "posix", reason="file modes are POSIX")
def test_esf_serve_keeps_the_access_key_out_of_output_that_is_not_a_terminal(tmp_path, monkeypatch, capsys):
    import uvicorn

    data = tmp_path / "data"
    monkeypatch.setenv("ESF_DATA_DIR", str(data))
    monkeypatch.setenv("ESF_ACCESS_TOKEN", "the-key-0123456789")
    monkeypatch.setattr(os, "umask", lambda mask: 0o022)
    monkeypatch.setattr(logging.getLogger("uvicorn.access"), "filters", [])
    assert not cli.to_terminal()  # captured, as `esf serve > esf.log` would be
    address = data / "address.txt"
    address.parent.mkdir()
    address.write_text("left by an earlier run")
    address.chmod(0o644)
    announced, seen = threading.Event(), []
    announce = cli.announce
    monkeypatch.setattr(cli, "announce", lambda *args, **kwargs: (announce(*args, **kwargs), announced.set()))

    def run(server, sockets=None):
        server.started = True
        assert announced.wait(5)
        seen.append((address.read_text(), stat.S_IMODE(address.stat().st_mode)))

    monkeypatch.setattr(uvicorn.Server, "run", run)
    cli.main(["serve", "--no-browser", "--host", "localhost", "--port", "8123"])
    out = capsys.readouterr().out
    assert "the-key" not in out and "#key=" not in out
    assert out.strip().splitlines()[-1] == (f"Effect Size Finder is running at http://127.0.0.1:8123/. The address to open, with its "
                                            f"access key, is in {address}, which only you can read.")
    # The file held the whole address, for this user only, while the server ran; it is gone once it stops.
    assert seen == [("http://127.0.0.1:8123/#key=the-key-0123456789\n", 0o600)]
    assert not address.exists()

    # A launch that fails to start (its port taken by a server already running, say) leaves that server's
    # address file alone: it is written only once a launch is up, and removed only by the launch that wrote it.
    address.write_text("http://127.0.0.1:8123/#key=the-running-servers-key\n")
    announced.clear()

    def taken(server, sockets=None):
        server.should_exit = True  # uvicorn gives up: the address is never announced

    monkeypatch.setattr(uvicorn.Server, "run", taken)
    with pytest.raises(SystemExit):
        cli.main(["serve", "--no-browser", "--port", "8123"])
    assert announced.wait(5)
    assert address.read_text() == "http://127.0.0.1:8123/#key=the-running-servers-key\n"
    assert "Effect Size Finder is running" not in capsys.readouterr().out


def test_the_access_log_leaves_out_the_sign_in_callbacks_code(tmp_path, monkeypatch):
    import uvicorn

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(make_client(tmp_path).app, host="127.0.0.1", port=port, log_level="info"))
    lines = []
    capture = logging.Handler()
    capture.emit = lambda record: lines.append(record.getMessage())
    access = logging.getLogger("uvicorn.access")
    monkeypatch.setattr(access, "handlers", [capture])
    monkeypatch.setattr(access, "filters", [cli.HideCallbackQuery()])
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    try:
        assert httpx.get(f"http://127.0.0.1:{port}/auth/callback?code=AUTH-CODE-123&state=STATE-456").status_code == 400
        assert httpx.get(f"http://127.0.0.1:{port}/api/projects?shown=yes").status_code == 200
    finally:
        server.should_exit = True
        thread.join(timeout=5)
    assert any('"GET /auth/callback?<redacted> HTTP/1.1" 400' in line for line in lines), lines
    assert not any("AUTH-CODE" in line or "STATE-456" in line for line in lines)
    assert any('"GET /api/projects?shown=yes HTTP/1.1" 200' in line for line in lines)


# -- who counts as another computer, and keys from there --------------------------------------


def test_a_request_from_no_address_counts_as_another_computer(tmp_path):
    session, _, _ = signed_in(tmp_path)
    app = make_client(tmp_path, chatgpt=session).app
    unnamed = TestClient(app, base_url=BASE)  # the test client's own peer, "testclient", is not an address
    assert unnamed.get("/api/chatgpt").json() == {"status": "connected", "remote": True}
    assert unnamed.post("/api/chatgpt/sign-out").status_code == 403
    response = unnamed.post("/api/projects", json={"paragraph": PARAGRAPH}, headers={"X-Model-Provider": "chatgpt"})
    assert response.status_code == 403 and session.state()["status"] == "connected"

    # uvicorn on a Unix socket gives no peer at all; a proxy's headers are looked at first all the same.
    def request(*headers):
        return Request({"type": "http", "method": "GET", "path": "/api/chatgpt", "query_string": b"", "client": None,
                        "headers": [(b"host", b"127.0.0.1:8000"), *headers]})

    assert _remote(request()) is True
    assert _remote(request((b"x-forwarded-for", b"203.0.113.9"))) is True


def test_keys_from_another_computer_are_taken_only_over_https(tmp_path):
    keys = []
    client = make_client(tmp_path, llm_for_key=lambda key, *models: keys.append(key) or FakeLLM(),
                         llm_for_openai=lambda key, *models: keys.append(key) or FakeLLM())
    lan = TestClient(client.app, base_url="http://192.168.1.10:8000", client=("192.168.1.20", 50000))
    proxied = TestClient(client.app, base_url=BASE, client=LOCAL, headers={"X-Forwarded-For": "203.0.113.9"})
    for other in (lan, proxied):
        for headers in ({"X-Anthropic-Api-Key": "sk-ant-test"}, {"X-Model-Provider": "openai", "X-OpenAI-Api-Key": "sk-proj-test"},
                        {"X-Model-Provider": "chatgpt", "X-OpenAI-Api-Key": "sk-proj-test"}):
            response = other.post("/api/projects", json={"paragraph": PARAGRAPH}, headers=headers)
            assert response.status_code == 400, headers
            assert response.json()["detail"] == "This server takes API keys from other computers only over HTTPS."
    assert keys == [] and client.get("/api/projects").json() == []
    # Over HTTPS (here, a TLS proxy's word for it) the key is taken; and from this computer, over HTTP too.
    secure = TestClient(client.app, base_url="https://192.168.1.10", client=("192.168.1.20", 50000))
    project_id = secure.post("/api/projects", json={"paragraph": PARAGRAPH}, headers={"X-Anthropic-Api-Key": "sk-ant-test"}).json()["id"]
    wait_for(secure, project_id, {"spec_ready"})
    assert client.post(f"/api/projects/{project_id}/run", headers={"X-Anthropic-Api-Key": "sk-ant-here"}).status_code == 202
    assert keys == ["sk-ant-test", "sk-ant-here"]
    wait_for(client, project_id, {"done"})

    # A key too long to be one is refused with a fixed message: never quoted back for the page to show.
    long_key = "sk-ant-api03-" + "k" * 600
    response = client.post("/api/projects", json={"paragraph": PARAGRAPH}, headers={"X-Anthropic-Api-Key": long_key})
    assert response.status_code == 400 and response.json() == {"detail": "The API key is too long. Paste only the key itself."}
    assert "kkkk" not in response.text and keys == ["sk-ant-test", "sk-ant-here"]


# -- the server's own Claude credential ------------------------------------------------------


def test_without_a_credential_of_its_own_the_server_asks_for_a_key(tmp_path, monkeypatch):
    profile_on_disk(tmp_path / "home", monkeypatch)  # which the Anthropic SDK alone would use
    keyed = []
    client = make_client(tmp_path, default_llm=True, llm_for_key=lambda key, *models: keyed.append(key) or FakeLLM())
    assert client.app.state.pipeline.llm.client is None
    assert client.get("/api/config").json()["server_has_key"] is False
    for headers in ({}, {"X-Model-Provider": "anthropic", "X-Manager-Model": "claude-sonnet-5-5"}):
        response = client.post("/api/projects", json={"paragraph": PARAGRAPH}, headers=headers)
        assert response.status_code == 409 and response.json()["detail"] == NO_KEY == "Enter your Anthropic API key first, on the Agents page."
    assert client.get("/api/projects").json() == []
    # A key from the page still runs the job.
    project_id = client.post("/api/projects", json={"paragraph": PARAGRAPH}, headers={"X-Anthropic-Api-Key": "sk-ant-test"}).json()["id"]
    wait_for(client, project_id, {"spec_ready"})
    assert keyed == ["sk-ant-test"]

    # The two documented variables are what make a server key, and what the page is told of.
    for variable in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"):
        monkeypatch.setenv(variable, "sk-ant-server")
        server = make_client(tmp_path / variable, default_llm=True)
        assert server.get("/api/config").json()["server_has_key"] is True
        assert server.app.state.pipeline.llm.client.credentials is None  # not the profile


# -- exports and the ChatGPT sign-in ---------------------------------------------------------------


def test_text_a_spreadsheet_would_run_is_exported_as_text(tmp_path):
    assert [csv_cell(v) for v in ("=1+1", "+cmd", "-x", "@SUM(A1)", "\tx", "\rx", "-", "=")] == [
        "'=1+1", "'+cmd", "'-x", "'@SUM(A1)", "'\tx", "'\rx", "'-", "'="]
    assert [csv_cell(v) for v in ("-0.42", "+3", "-.5", "1e-3", "-2.5E+4", "plain", " =A1", -2.5, 3, None)] == [
        "-0.42", "+3", "-.5", "1e-3", "-2.5E+4", "plain", " =A1", -2.5, 3, None]

    client = make_client(tmp_path)
    store = client.app.state.store
    project_id = store.create_project(PARAGRAPH)
    value = {"group": "@App", "statistic": "mean_difference", "value": -2.5, "ci_level": None, "describes": "+ change in PHQ-9",
             "quote": "-2.5", "location": '=HYPERLINK("https://evil.example","Table 2")'}
    result = {"outcome_measure": "=1+1", "timepoint": "-12 weeks", "analysis_population": "itt", "adjusted": "unadjusted",
              "relevance": "direct", "confidence": "high", "concerns": ["\tsmall sample"], "values": [value]}
    paper_id = store.add_paper(project_id, source="pubmed", ext_id="1", title="+Mindfulness for students", year="2021",
                               status="extracted", extraction={"results": [result]},
                               verification={"values": [[{"quote": "found", "number_in_quote": True}]], "flags": []})
    store.set_decision(paper_id, 0, "include", "=cmd|' /C calc'!A0")
    [row] = csv.DictReader(io.StringIO(client.get(f"/api/projects/{project_id}/export.csv?included_only=true").text))
    assert (row["title"], row["outcome_measure"], row["timepoint"], row["group"], row["describes"]) == (
        "'+Mindfulness for students", "'=1+1", "'-12 weeks", "'@App", "'+ change in PHQ-9")
    assert row["location"] == "'" + value["location"] and row["concerns"] == "'\tsmall sample"
    assert row["decision_reason"] == "'=cmd|' /C calc'!A0"
    # Numbers stay numbers, negative ones too, and so does a quote that is only a number.
    assert (row["value"], row["quote"], row["year"], row["result"]) == ("-2.5", "-2.5", "2021", "1")
    # The JSON export keeps the text as the paper has it.
    assert client.get(f"/api/projects/{project_id}/export.json").json()["papers"][0]["title"] == "+Mindfulness for students"


def test_another_chatgpt_account_signs_in_on_a_new_registration(tmp_path):
    session, api, _ = signed_in(tmp_path)
    client = make_client(tmp_path, chatgpt=session)
    saved = (tmp_path / "settings" / "chatgpt.json").read_text()
    # Only this computer's own page may start it.
    remote = TestClient(client.app, base_url=BASE, client=("192.0.2.2", 50000))
    assert remote.post("/api/chatgpt/another-account").status_code == 400
    assert client.post("/api/chatgpt/another-account", headers={"Origin": "https://evil.example"}).status_code == 403
    assert (tmp_path / "settings" / "chatgpt.json").read_text() == saved and session.state()["pending"] is False

    url = client.post("/api/chatgpt/another-account").json()["url"]
    params = dict(httpx.URL(url).params)
    assert params["client_id"] == REGISTRATION_ENTRY and params["agent_name_hint"] == APP_NAME and "login_hint" not in params
    state = client.get("/api/chatgpt").json()  # Ada's sign-in serves until Bob's finishes
    assert (state["status"], state["email"], state["pending"], state["account_changed"]) == ("connected", "ada@example.com", True, False)
    as_bob(api)
    assert client.get(f"/auth/callback?{api.authorize(url)}").status_code == 200
    state = client.get("/api/chatgpt").json()
    assert (state["email"], state["account_changed"]) == ("bob@example.com", True)


def test_after_chatgpt_withholds_the_plan_the_next_sign_in_asks_for_it_again(tmp_path):
    session, api, _ = new_session(tmp_path)
    client = make_client(tmp_path, chatgpt=session)
    api.scope = WITHOUT_PLAN
    url = client.post("/api/chatgpt/sign-in").json()["url"]
    assert "prompt" not in dict(httpx.URL(url).params)
    page = client.get(f"/auth/callback?{api.authorize(url)}")
    assert page.status_code == 400 and "allow Effect Size Finder to use your plan" in page.text
    state = client.get("/api/chatgpt").json()
    assert (state["status"], state["reconsent"]) == ("disconnected", True) and "Plus or Pro" in state["error"]
    api.scope = SCOPES
    url = client.post("/api/chatgpt/sign-in").json()["url"]
    assert dict(httpx.URL(url).params)["prompt"] == "consent"
    assert client.get(f"/auth/callback?{api.authorize(url)}").status_code == 200
    state = client.get("/api/chatgpt").json()
    assert (state["status"], state["reconsent"], state["error"]) == ("connected", False, None)


def test_the_page_hears_of_a_sign_in_that_never_came_back_and_of_a_renewal_not_allowed_yet(tmp_path):
    session, api, clock = new_session(tmp_path)
    sent = []  # what reached the Responses API
    openai_api = httpx.Client(transport=httpx.MockTransport(lambda request: sent.append(request) or httpx.Response(500)))
    client = make_client(tmp_path, chatgpt=session,
                         llm_for_chatgpt=lambda chatgpt: OpenAILLM(Settings(), chatgpt.access_token, client=openai_api))
    client.post("/api/chatgpt/sign-in")
    clock.now += PENDING_SECONDS + 1
    state = client.get("/api/chatgpt").json()
    assert (state["pending"], state["error"]) == (False, NO_CALLBACK)

    # ChatGPT lets the token run out before it allows a renewal.
    api.token_changes = {"earliest_refresh_at": clock.now + 3600 + REFRESH_WAIT + 40}
    url = client.post("/api/chatgpt/sign-in").json()["url"]
    assert client.get("/api/chatgpt").json()["error"] is None
    client.get(f"/auth/callback?{api.authorize(url)}")
    clock.now += 3600
    state = client.get("/api/chatgpt?refresh=true").json()
    assert (state["status"], state["error"]) == ("connected", REFRESH_NOT_READY)
    # A job started meanwhile says so too, without reaching OpenAI.
    project_id = client.post("/api/projects", json={"paragraph": PARAGRAPH}, headers={"X-Model-Provider": "chatgpt"}).json()["id"]
    assert REFRESH_NOT_READY in wait_for(client, project_id, {"error"})["error"] and sent == []


def test_a_job_on_the_plan_waits_longer_for_a_renewal_than_the_page(tmp_path, monkeypatch):
    session, api, clock = new_session(tmp_path)
    api.token_changes = {"earliest_refresh_at": clock.now + 3600 + JOB_REFRESH_WAIT + 40}
    session.finish(api.authorize(session.start(PORT)), HOST)
    waits = []
    access_token = session.access_token
    monkeypatch.setattr(session, "access_token", lambda max_wait=REFRESH_WAIT: waits.append(max_wait) or access_token(max_wait))
    client = make_client(tmp_path, chatgpt=session)  # the server's own models for the plan
    clock.now += 3600
    assert client.get("/api/chatgpt?refresh=true").json()["error"] == REFRESH_NOT_READY
    assert waits == [REFRESH_WAIT]
    # Past even a job's wait, the job stops at once and says why, without reaching OpenAI.
    project_id = client.post("/api/projects", json={"paragraph": PARAGRAPH}, headers={"X-Model-Provider": "chatgpt"}).json()["id"]
    assert wait_for(client, project_id, {"error"})["error"].endswith(REFRESH_NOT_READY)
    assert waits == [REFRESH_WAIT, JOB_REFRESH_WAIT] and clock.slept == [] and api.refreshes == 0
    # Within a job's wait, the token comes after it: the job's model calls go on with it.
    clock.now += 40 + 1
    llm = plan_llm(Settings(), session)
    assert llm.responses.token() == "at-2" and clock.slept == [JOB_REFRESH_WAIT - 1] and api.refreshes == 1
