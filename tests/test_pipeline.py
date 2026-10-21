import httpx

from esf.config import Settings
from esf.pipeline import Pipeline
from esf.server import export_rows
from esf.sources import Sources
from esf.store import Store
from fixtures import FakeLLM, LiteratureAPIs, mock_client


def make_pipeline(tmp_path, **overrides):
    options = {"contact_email": "test@example.com", "audit_fraction": 0.0, "worker_concurrency": 2, **overrides}
    settings = Settings(data_dir=tmp_path, **options)
    store = Store(tmp_path / "test.sqlite3")
    llm = FakeLLM()
    return Pipeline(store, llm, Sources(settings, client=mock_client(LiteratureAPIs())), settings), store, llm


def run_all(tmp_path, **overrides):
    pipeline, store, llm = make_pipeline(tmp_path, **overrides)
    project_id = store.create_project("We plan a randomized trial of a mindfulness app versus waitlist for PHQ-9 at 12 weeks.")
    pipeline.read_spec(project_id)
    assert store.project(project_id)["status"] == "spec_ready"
    pipeline.run(project_id)
    return pipeline, store, llm, project_id


def test_search_screen_extract_end_to_end(tmp_path):
    pipeline, store, llm, project_id = run_all(tmp_path)
    project = store.project(project_id)
    assert project["status"] == "done", project["error"]
    assert project["search_plan"]["pubmed_queries"] == ["mindfulness[tiab] AND depress*[tiab]"]

    papers = {p["ext_id"]: p for p in store.papers(project_id)}
    assert set(papers) == {"11111111", "22222222", "33333333", "44444444", "NCT01234567"}
    assert papers["33333333"]["status"] == "screened_out"
    assert papers["NCT01234567"]["found_by"] == ["clinicaltrials: mindfulness AND depression"]

    kinds = {k: p["fulltext_kind"] for k, p in papers.items() if p["status"] == "extracted"}
    assert kinds == {"11111111": "pmc_xml", "22222222": "oa_pdf", "44444444": "abstract", "NCT01234567": "registry"}

    # Clean extractions stand as the worker made them.
    for key in ("11111111", "22222222", "NCT01234567"):
        assert papers[key]["extracted_by"] == "worker", (key, papers[key]["escalation"])
        assert papers[key]["verification"]["flags"] == []
    # The abstract-only pilot had a number that is not in its quote: the manager redid it.
    pilot = papers["44444444"]
    assert pilot["extracted_by"] == "manager"
    assert any("7.7" in r and "not in its quote" in r for r in pilot["escalation"]["reasons"])
    assert pilot["verification"]["flags"] == []
    assert [v["value"] for v in pilot["extraction"]["results"][0]["values"]] == [7.9, 3.8, 20, 9.6, 4.1, 21]

    assert llm.calls.count("spec") == 1 and llm.calls.count("search") == 1
    assert llm.calls.count("screen") == 5
    assert llm.calls.count("extract") == 4 and llm.calls.count("adjudicate") == 1
    assert "8.1 (4.2)" in pipeline.source_text(project_id, papers["11111111"]["id"])


def test_decisions_drive_the_export(tmp_path):
    _, store, _, project_id = run_all(tmp_path)
    papers = {p["ext_id"]: p for p in store.papers(project_id)}
    store.set_decision(papers["11111111"]["id"], 0, "include")
    store.set_decision(papers["NCT01234567"]["id"], -1, "exclude", "Same trial as another record")
    store.set_decision(papers["NCT01234567"]["id"], 0, "include")  # a result's decision beats its paper's
    store.set_decision(papers["44444444"]["id"], -1, "exclude", "Different population")

    included = export_rows(store, project_id, included_only=True)
    assert {r["id"] for r in included} == {"11111111", "NCT01234567"}
    everything = export_rows(store, project_id, included_only=False)
    pilot = [r for r in everything if r["id"] == "44444444"]
    assert pilot and all(r["decision"] == "exclude" and r["decision_reason"] == "Different population" for r in pilot)
    row = next(r for r in included if r["id"] == "11111111" and r["statistic"] == "sd" and r["group"] == "App")
    assert row["value"] == 4.2 and row["quote_check"] == "exact" and row["text_used"] == "pmc_xml"


def test_audit_sends_clean_papers_to_the_manager(tmp_path):
    _, store, llm, project_id = run_all(tmp_path, audit_fraction=1.0)
    audited = [p for p in store.papers(project_id) if (p["escalation"] or {}).get("audit")]
    assert len(audited) == 3
    assert llm.calls.count("adjudicate") == 4


def test_one_failing_query_does_not_stop_the_search(tmp_path):
    pipeline, store, llm = make_pipeline(tmp_path)
    handler = LiteratureAPIs()

    def picky(request):
        if request.url.path.endswith("esearch.fcgi") and b"BROKEN" in request.content:
            return httpx.Response(400, text="bad query")
        return handler(request)

    pipeline.sources = Sources(pipeline.settings, client=httpx.Client(transport=httpx.MockTransport(picky)))
    plan_search = llm.plan_search

    def plan_with_a_bad_query(spec, toolbox, sink=None):
        plan = plan_search(spec, toolbox, sink)
        return plan.model_copy(update={"pubmed_queries": ["BROKEN", *plan.pubmed_queries]})

    llm.plan_search = plan_with_a_bad_query
    project_id = store.create_project("A paragraph long enough to be a study description.")
    pipeline.read_spec(project_id)
    pipeline.run(project_id)
    project = store.project(project_id)
    assert project["status"] == "done"
    assert project["search_plan"]["failed_queries"][0].startswith("pubmed: BROKEN")
    assert len(store.papers(project_id)) == 5


def test_a_failed_worker_is_never_dropped(tmp_path):
    pipeline, store, llm = make_pipeline(tmp_path)

    def broken(*args, **kwargs):
        raise RuntimeError("boom")

    llm.extract = broken
    llm.adjudicate = broken
    llm.screen = broken
    project_id = store.create_project("A paragraph long enough to be a study description.")
    pipeline.read_spec(project_id)
    pipeline.run(project_id)
    papers = store.papers(project_id)
    assert store.project(project_id)["status"] == "done"
    # Unscreenable records go forward as "unsure"; unextractable ones wait for a person.
    assert {p["status"] for p in papers} == {"needs_manual"}
    assert all("Neither model" in p["error"] for p in papers)
