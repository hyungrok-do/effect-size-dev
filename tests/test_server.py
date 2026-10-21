import csv
import io
import time

from fastapi.testclient import TestClient

from esf.config import REPO_ROOT, Settings
from esf.server import create_app
from esf.sources import Sources
from esf.store import Store
from fixtures import SPEC, FakeLLM, LiteratureAPIs, mock_client


def make_client(tmp_path):
    settings = Settings(data_dir=tmp_path, web_dir=REPO_ROOT / "web", contact_email="test@example.com", audit_fraction=0.0)
    app = create_app(
        settings,
        llm=FakeLLM(),
        sources=Sources(settings, client=mock_client(LiteratureAPIs())),
        store=Store(tmp_path / "test.sqlite3"),
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
    assert project["spec"]["outcomes"] == ["PHQ-9"]

    edited = {**SPEC.model_dump(), "timepoint": "8 to 12 weeks"}
    assert client.put(f"/api/projects/{project_id}/spec", json=edited).status_code == 200
    assert client.post(f"/api/projects/{project_id}/run").status_code == 202
    project = wait_for(client, project_id, {"done", "error"})
    assert project["status"] == "done", project["error"]
    assert project["spec"]["timepoint"] == "8 to 12 weeks"
    assert project["counts"] == {"extracted": 4, "screened_out": 1}

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


def test_unknown_ids(tmp_path):
    client = make_client(tmp_path)
    assert client.get("/api/projects/nope").status_code == 404
    assert client.get("/api/projects/nope/papers").status_code == 404
