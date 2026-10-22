import httpx

from esf.config import Settings
from esf.pipeline import Pipeline
from esf.schemas import SearchLevel
from esf.server import export_rows
from esf.sources import Sources
from esf.store import Store
from esf import prompts
from fixtures import CITED_TRIAL, NOT_A_STUDY, PLAN, PMC_ONLY, PREPRINT, REVIEW, FakeLLM, LiteratureAPIs, mock_client


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
    assert [lv["level"] for lv in project["search_plan"]["levels"]] == ["primary", "reviews", "full_text"]

    papers = {p["ext_id"]: p for p in store.papers(project_id)}
    assert set(papers) == {"11111111", "22222222", "33333333", "44444444", "NCT01234567", REVIEW, CITED_TRIAL, PMC_ONLY, PREPRINT}
    assert papers["33333333"]["status"] == "screened_out"
    assert papers["NCT01234567"]["found_by"] == ["ClinicalTrials.gov: mindfulness AND depression"]

    kinds = {k: p["fulltext_kind"] for k, p in papers.items() if p["status"] == "extracted"}
    assert kinds == {
        "11111111": "pmc_xml", "22222222": "oa_pdf", "44444444": "abstract", "NCT01234567": "registry",
        REVIEW: "abstract", CITED_TRIAL: "abstract", PMC_ONLY: "abstract", PREPRINT: "abstract",
    }

    # Clean extractions stand as the worker made them.
    for key in ("11111111", "22222222", "NCT01234567", REVIEW, CITED_TRIAL, PMC_ONLY, PREPRINT):
        assert papers[key]["extracted_by"] == "worker", (key, papers[key]["escalation"])
        assert papers[key]["verification"]["flags"] == []
    # The abstract-only pilot had a number that is not in its quote: the manager redid it.
    pilot = papers["44444444"]
    assert pilot["extracted_by"] == "manager"
    assert any("7.7" in r and "not in its quote" in r for r in pilot["escalation"]["reasons"])
    assert pilot["verification"]["flags"] == []
    assert [v["value"] for v in pilot["extraction"]["results"][0]["values"]] == [7.9, 3.8, 20, 9.6, 4.1, 21]

    assert llm.calls.count("spec") == 1 and llm.calls.count("search") == 1
    assert llm.calls.count("screen") == 9
    assert llm.calls.count("extract") == 8 and llm.calls.count("adjudicate") == 1
    assert "8.1 (4.2)" in pipeline.source_text(project_id, papers["11111111"]["id"])


def test_the_search_runs_from_the_top_down(tmp_path):
    _, store, _, project_id = run_all(tmp_path)
    papers = store.papers(project_id)
    # The plan listed the primary level first; the reviews still run first, and the registry
    # records are interleaved with the articles.
    assert [p["ext_id"] for p in papers][:3] == [REVIEW, "NCT01234567", "11111111"]
    found_in = {p["ext_id"]: p["found_in"] for p in papers}
    assert found_in[REVIEW] == ["reviews"] and found_in["11111111"] == ["primary"]
    assert found_in["NCT01234567"] == ["registry"] and found_in[CITED_TRIAL] == ["cited"]
    # The full-text search found one paper PubMed had already found, and two it hadn't.
    assert found_in["22222222"] == ["primary", "full_text"]
    assert found_in[PMC_ONLY] == ["full_text"] and found_in[PREPRINT] == ["full_text"]

    # The trial only the review cites was added after screening; the narrative review it also
    # cites was not, and the trial already found was not added twice.
    cited = next(p for p in papers if p["ext_id"] == CITED_TRIAL)
    assert cited["found_by"] == [f"Cited by the included reviews: {next(p for p in papers if p['ext_id'] == REVIEW)['title']} (PMID {REVIEW})"]
    assert NOT_A_STUDY not in found_in
    # Every record a review cites knows it, the ones found before the review too: the overlap check uses this.
    cited_by = {p["ext_id"]: p["cited_by"] for p in papers}
    assert cited_by[CITED_TRIAL] == [REVIEW] and cited_by["11111111"] == [REVIEW] and cited_by["22222222"] == []

    log = store.project(project_id)["search_log"]
    assert [(e["level"], e["source"], e.get("hits"), e.get("taken"), e["added"]) for e in log] == [
        ("reviews", "pubmed", 1, 1, 1), ("primary", "pubmed", 4, 4, 4), ("full_text", "europepmc", 4, 3, 2),
        ("registry", "clinicaltrials", 1, 1, 1), ("cited", "pubmed", None, None, 1),
    ]
    assert log[0]["query"] == PLAN.levels[1].query
    assert {k: log[4][k] for k in ("reviews", "cited", "already", "not_studies")} == {
        "reviews": 1, "cited": 3, "already": 1, "not_studies": 1,
    }


def test_no_included_review_means_nothing_to_follow(tmp_path):
    pipeline, store, llm = make_pipeline(tmp_path)
    plan_search = llm.plan_search
    llm.plan_search = lambda *a, **k: plan_search(*a, **k).model_copy(update={"levels": [PLAN.levels[0]]})
    project_id = store.create_project("A paragraph long enough to be a study description.")
    pipeline.read_spec(project_id)
    pipeline.run(project_id)
    log = store.project(project_id)["search_log"]
    assert log[-1] == {"level": "cited", "source": "pubmed", "reviews": 0, "cited": 0, "already": 0, "added": 0, "not_studies": 0}
    assert REVIEW not in {p["ext_id"] for p in store.papers(project_id)}


def test_the_follow_up_is_capped(tmp_path):
    _, store, _, project_id = run_all(tmp_path, max_cited=0)
    assert CITED_TRIAL not in {p["ext_id"] for p in store.papers(project_id)}
    _, store, _, project_id = run_all(tmp_path / "again", max_reviews_followed=0)
    assert store.project(project_id)["search_log"][-1]["reviews"] == 0


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
    assert len(audited) == 7
    assert llm.calls.count("adjudicate") == 8


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
        return plan.model_copy(update={"levels": [SearchLevel(level="wider", query="BROKEN", why=""), *plan.levels]})

    llm.plan_search = plan_with_a_bad_query
    project_id = store.create_project("A paragraph long enough to be a study description.")
    pipeline.read_spec(project_id)
    pipeline.run(project_id)
    project = store.project(project_id)
    assert project["status"] == "done"
    assert project["search_plan"]["failed_queries"][0].startswith("Studies in related populations: BROKEN")
    assert [e["level"] for e in project["search_log"] if "error" in e] == ["wider"]
    assert len(store.papers(project_id)) == 9


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


def test_cited_records_that_cannot_be_fetched_do_not_stop_the_search(tmp_path, monkeypatch):
    monkeypatch.setattr("esf.sources.time.sleep", lambda _: None)  # no waiting between retries
    pipeline, store, _ = make_pipeline(tmp_path)
    handler = LiteratureAPIs()

    def no_cited_records(request):
        if request.url.path.endswith("efetch.fcgi") and CITED_TRIAL.encode() in request.content:
            return httpx.Response(500, text="unavailable")
        return handler(request)

    pipeline.sources = Sources(pipeline.settings, client=httpx.Client(transport=httpx.MockTransport(no_cited_records)))
    project_id = store.create_project("A paragraph long enough to be a study description.")
    pipeline.read_spec(project_id)
    pipeline.run(project_id)
    project = store.project(project_id)
    assert project["status"] == "done", project["error"]
    cited = project["search_log"][-1]
    assert cited["added"] == 0 and cited["failed"][0].startswith("fetching the 2 cited records")
    assert store.status_counts(project_id) == {"extracted": 7, "screened_out": 1}


def test_papers_only_in_europe_pmc(tmp_path):
    pipeline, store, llm, project_id = run_all(tmp_path)
    papers = {p["ext_id"]: p for p in store.papers(project_id)}
    pmc, preprint = papers[PMC_ONLY], papers[PREPRINT]
    assert (pmc["source"], pmc["pmid"], pmc["pmcid"], pmc["year"]) == ("europepmc", None, PMC_ONLY, "2023")
    assert pmc["title"] == "Mindfulness app use and depressive symptoms in nursing students: a randomised trial"
    assert pmc["abstract"].startswith("Results PHQ-9 at 8 weeks")  # HTML tags dropped
    assert (preprint["journal"], preprint["pubtypes"], preprint["doi"]) == ("medRxiv", ["Preprint"], "10.1101/2024.01.01.24300001")
    assert pmc["fulltext_url"] == f"https://europepmc.org/article/PMC/{PMC_ONLY}"
    assert preprint["fulltext_url"] == f"https://europepmc.org/article/PPR/{PREPRINT}"
    # Screening is told which records the full-text search found, so an abstract that doesn't name
    # the outcome is not held against them.
    marked = [t for t in llm.screened if prompts.FOUND_BY_FULL_TEXT in t]
    assert len(marked) == 3 and all(("Brief mindfulness" in t) or ("nursing" in t) or ("smartphone" in t) for t in marked)


def test_overlap_is_checked_and_the_summary_describes_what_was_kept(tmp_path):
    pipeline, store, llm, project_id = run_all(tmp_path)
    keys = {p["ext_id"]: f"R{p['id']}" for p in store.papers(project_id)}
    report = store.project(project_id)["overlap"]["report"]
    groups = {g["relation"]: g for g in report["groups"]}
    # A trial and its registry record are one study; the meta-analysis cites trials also in the set.
    assert groups["same_study"]["records"] == [keys["NCT01234567"], keys["11111111"]]
    assert groups["same_study"]["use"] == keys["11111111"]
    assert groups["review_includes_study"]["records"] == [keys[REVIEW], keys["11111111"], keys[CITED_TRIAL]]
    sent = {r["key"]: r for r in llm.overlap_records}
    assert sent[keys["11111111"]]["authors"] == "Kim J, Lee S, Park H, et al."
    assert sent[keys["11111111"]]["cited_by"] == [keys[REVIEW]] and sent[keys["11111111"]]["nct_ids"] == ["NCT01234567"]

    pilot = next(p for p in store.papers(project_id) if p["ext_id"] == "44444444")
    store.set_decision(pilot["id"], 0, "exclude")
    pipeline.summarize(project_id)
    summary = store.project(project_id)["summary"]
    assert summary["status"] == "done", summary
    evidence = summary["evidence"]
    # The pilot's only result was excluded, so neither it nor its values are described.
    assert keys["44444444"] not in {v["record"] for v in evidence["values"]} | {r["key"] for r in evidence["records"]}
    # The registry record is kept, marked as the same participants as its paper; nothing is pooled.
    registry = [v for v in evidence["values"] if v["record"] == keys["NCT01234567"]]
    assert registry and all(v["same_participants_as"] == keys["11111111"] for v in registry)
    assert all(v["same_participants_as"] is None for v in evidence["values"] if v["record"] != keys["NCT01234567"])
    assert set(evidence) == {"records", "values"} and [v["key"] for v in evidence["values"]][:2] == ["V1", "V2"]
    need = summary["result"]["needs"][0]
    assert need["need"] == "The difference and its SD" and need["values"]
    assert {evidence["values"][int(k[1:]) - 1]["statistic"] for k in need["values"]} <= {"mean", "sd", "n"}
    assert summary["basis"][0][0] == min(p["id"] for p in store.papers(project_id) if p["status"] == "extracted")


def test_nothing_kept_means_no_summary(tmp_path):
    pipeline, store, llm, project_id = run_all(tmp_path)
    for paper in store.papers(project_id):
        store.set_decision(paper["id"], -1, "exclude")
    pipeline.summarize(project_id)
    summary = store.project(project_id)["summary"]
    assert summary["status"] == "error" and "nothing to summarize" in summary["error"]
    assert "summarize" not in llm.calls
