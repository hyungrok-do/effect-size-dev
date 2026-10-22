import csv
import io
import time

from fastapi.testclient import TestClient

from esf.config import REPO_ROOT, Settings
from esf.server import create_app
from esf.sources import Sources
from esf.store import Store
from fixtures import SPEC, FakeLLM, LiteratureAPIs, mock_client


def make_client(tmp_path, llm=None, llm_for_key=None):
    settings = Settings(data_dir=tmp_path, web_dir=REPO_ROOT / "web", contact_email="test@example.com", audit_fraction=0.0)
    app = create_app(
        settings,
        llm=llm or FakeLLM(),
        sources=Sources(settings, client=mock_client(LiteratureAPIs())),
        store=Store(tmp_path / "test.sqlite3"),
        llm_for_key=llm_for_key,
    )
    return TestClient(app)


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

    def llm_for_key(key):
        keys.append(key)
        return keyed

    client = make_client(tmp_path, llm=default, llm_for_key=llm_for_key)
    assert client.get("/api/config").json()["server_has_key"] is False

    headers = {"X-Anthropic-Api-Key": "  sk-ant-test-123  "}
    project_id = client.post("/api/projects", json={"paragraph": "A randomized trial of a mindfulness app for PHQ-9."}, headers=headers).json()["id"]
    wait_for(client, project_id, {"spec_ready"})
    assert keys == ["sk-ant-test-123"] and keyed.calls == ["spec"] and default.calls == []

    client.post(f"/api/projects/{project_id}/run", headers=headers)
    wait_for(client, project_id, {"done"})
    assert "search" in keyed.calls and default.calls == []

    # Without a key the server falls back to its own credentials.
    other = client.post("/api/projects", json={"paragraph": "Another randomized trial description here."}).json()["id"]
    wait_for(client, other, {"spec_ready"})
    assert default.calls == ["spec"]

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-server")
    assert client.get("/api/config").json()["server_has_key"] is True


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
    client = make_client(tmp_path, llm=FakeLLM(), llm_for_key=lambda key: keys.append(key) or llm)
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
    # The page's shared modules are served next to it.
    assert client.get("/lib/review-view.js").status_code == 200 and client.get("/lib/evidence.js").status_code == 200
