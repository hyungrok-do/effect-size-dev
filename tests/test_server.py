import csv
import io
import json
import re
import time

import anthropic
import httpx2
from fastapi.testclient import TestClient

from esf.config import MODEL_CHOICES, REPO_ROOT, Settings
from esf.llm import ClaudeLLM
from esf.server import create_app
from esf.sources import Sources
from esf.store import Store
from fixtures import SPEC, FakeLLM, LiteratureAPIs, mock_client, streamed
from test_chatgpt import new_session, signed_in

PARAGRAPH = "A randomized trial of a mindfulness app for PHQ-9."
BASE = "http://127.0.0.1:8000"  # the server answers only to its address or localhost


def make_client(tmp_path, llm=None, llm_for_key=None, llm_for_openai=None, chatgpt=None, llm_for_chatgpt=None, **settings):
    settings = Settings(data_dir=tmp_path, web_dir=REPO_ROOT / "web", contact_email="test@example.com", audit_fraction=0.0, **settings)
    app = create_app(
        settings,
        llm=llm or FakeLLM(),
        sources=Sources(settings, client=mock_client(LiteratureAPIs())),
        store=Store(tmp_path / "test.sqlite3"),
        llm_for_key=llm_for_key,
        llm_for_openai=llm_for_openai,
        chatgpt=chatgpt,
        llm_for_chatgpt=llm_for_chatgpt,
    )
    return TestClient(app, base_url=BASE)


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

    rows = list(csv.DictReader(io.StringIO(client.get(f"/api/projects/{project_id}/export.csv?included_only=true").text)))
    assert {r["id"] for r in rows} == {"11111111"} and len(rows) == 6
    assert client.get(f"/api/projects/{project_id}/export.json").json()["project"]["id"] == project_id

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
    proxied = TestClient(local.app, base_url=BASE, client=("127.0.0.1", 50000))
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
