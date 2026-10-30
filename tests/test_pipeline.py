import json

import anthropic
import httpx
import pytest

from esf.chatgpt import JOB_REFRESH_WAIT, REFRESH_NOT_READY, ChatGPTError
from esf.config import Settings
from esf.openai_llm import OpenAIError, OpenAILLM
from esf.pipeline import Pipeline, stops_the_run
from esf.schemas import SearchLevel, SearchSpec, upgrade_spec
from esf.server import export_rows, plan_llm
from esf.sources import Sources
from esf.store import Store
from esf import prompts
from fixtures import (
    CITED_TRIAL, NOT_A_STUDY, PLAN, PMC_ONLY, PREPRINT, REVIEW, FakeLLM, FakeOpenAI, LiteratureAPIs, mock_client,
)
from test_chatgpt import HOST, PORT, new_session


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

    # Then the manager read what each paper's numbers estimate, after the final extraction (the
    # pilot's after the manager redid it), and its quotes were checked against the paper's text.
    assert llm.calls.count("context") == 8
    assert llm.calls.index("context") > llm.calls.index("extract")
    assert all(p["context"]["status"] == "done" for p in papers.values() if p["status"] == "extracted")
    assert papers["33333333"]["context"] is None
    trial = papers["11111111"]["context"]
    [result] = trial["result"]["results"]
    assert (result["result"], result["model"], result["vs_plan"]) == (1, "ANCOVA adjusted for baseline", "same")
    assert result["quote"] == "Analyses followed the intention-to-treat principle."
    assert trial["checks"] == {"sample_size": None, "results": ["exact"]}
    assert papers[REVIEW]["context"]["result"]["results"][0]["vs_plan"] == "different"
    assert papers[REVIEW]["context"]["checks"] == {"sample_size": None, "results": [None]}
    assert len(papers["44444444"]["context"]["result"]["results"]) == len(pilot["extraction"]["results"])


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
    rec = summary["result"]["recommendations"][0]
    assert rec["status"] == "provisional" and rec["resolved"]["value"] == .3
    assert rec["resolved"]["basis_value"] == .35
    assert summary["spec_basis"] == store.project(project_id)["spec"]
    evidence = summary["evidence"]
    # The pilot's only result was excluded, so neither it nor its values are described.
    assert keys["44444444"] not in {v["record"] for v in evidence["values"]} | {r["key"] for r in evidence["records"]}
    # The registry record is kept, marked as the same participants as its paper; nothing is pooled.
    registry = [v for v in evidence["values"] if v["record"] == keys["NCT01234567"]]
    assert registry and all(v["same_participants_as"] == keys["11111111"] for v in registry)
    assert all(v["same_participants_as"] is None for v in evidence["values"] if v["record"] != keys["NCT01234567"])
    assert list(evidence) == ["records", "estimands", "values"]
    assert [v["key"] for v in evidence["values"]][:2] == ["V1", "V2"]
    # The manager's reading of each kept result is its estimand, named by the result's values.
    assert [e["key"] for e in evidence["estimands"]] == [f"E{i}" for i in range(1, len(evidence["records"]) + 1)]
    by_result = {e["result"]: e["key"] for e in evidence["estimands"]}
    assert all(v["estimand"] == by_result[v["result"]] for v in evidence["values"])
    trial = next(e for e in evidence["estimands"] if e["record"] == keys["11111111"])
    assert (trial["result"], trial["model"], trial["vs_plan"]) == (f"{keys['11111111']}:0", "ANCOVA adjusted for baseline", "same")
    assert keys["44444444"] not in {e["record"] for e in evidence["estimands"]}
    # Each kept record carries the design and the paper's own sample-size calculation as the manager read them.
    records = {r["key"]: r for r in evidence["records"]}
    assert records[keys[REVIEW]]["context"] == {
        "design": "meta-analysis of randomized trials", "sample_size": "", "sample_size_found": False, "notes": ""}
    assert all(r["context"]["design"] for r in records.values())
    need = summary["result"]["needs"][0]
    assert need["need"] == "The difference and its SD" and need["values"]
    assert {evidence["values"][int(k[1:]) - 1]["statistic"] for k in need["values"]} <= {"mean", "sd", "n"}
    assert need["estimands"].startswith("[V") and "estimates the planned difference" in need["estimands"]
    assert summary["basis"][0][0] == min(p["id"] for p in store.papers(project_id) if p["status"] == "extracted")
    assert summary["basis"][0][8]["design"]  # the context it was written with


def test_a_recommendation_on_a_result_read_as_a_different_estimand_is_provisional(tmp_path):
    pipeline, store, llm, project_id = run_all(tmp_path)
    review = next(p for p in store.papers(project_id) if p["ext_id"] == REVIEW)
    different = ("The manager read the selected result's estimand as different from the planned one: "
                 "standardized scale, not PHQ-9 points.")
    unread = "The manager has not read the selected result's estimand and model."

    def recommendation():
        pipeline.summarize(project_id)
        summary = store.project(project_id)["summary"]
        assert summary["status"] == "done", summary
        [rec] = summary["result"]["recommendations"]
        # The head agent chose the review's pooled effect, whose result the manager read.
        [key] = rec["choice"]["inputs"]
        value = next(v for v in summary["evidence"]["values"] if v["key"] == key)
        assert value["record"] == f"R{review['id']}" and value["statistic"] == "standardized_mean_difference"
        return rec, value, summary["evidence"]

    # The manager read it as a pooled standardized effect: a different estimand from the planned difference
    # in PHQ-9 points. The resolved number stays, as a provisional one, with the reason.
    rec, value, evidence = recommendation()
    assert next(e for e in evidence["estimands"] if e["key"] == value["estimand"])["vs_plan"] == "different"
    assert rec["status"] == "provisional" and rec["resolved"]["value"] == .3 and rec["resolved"]["basis_value"] == .35
    assert rec["validation_notes"][-1] == different and unread not in rec["validation_notes"]

    # Read as the same estimand, that note goes; the others stand.
    context = review["context"]
    same = {**context, "result": {**context["result"], "results": [
        {**r, "vs_plan": "same", "differences": []} for r in context["result"]["results"]]}}
    store.update_paper(review["id"], context=same)
    rec, value, _ = recommendation()
    assert value["estimand"] and different not in rec["validation_notes"] and unread not in rec["validation_notes"]
    assert rec["status"] == "provisional" and rec["validation_notes"]

    # Not read at all (an older project, or the reading failed): provisional until the manager reads it.
    store.update_paper(review["id"], context={"status": "error", "error": "overloaded"})
    rec, value, _ = recommendation()
    assert value["estimand"] is None and rec["status"] == "provisional" and rec["validation_notes"][-1] == unread
    assert rec["resolved"]["value"] == .3


def test_nothing_kept_means_no_summary(tmp_path):
    pipeline, store, llm, project_id = run_all(tmp_path)
    for paper in store.papers(project_id):
        store.set_decision(paper["id"], -1, "exclude")
    pipeline.summarize(project_id)
    summary = store.project(project_id)["summary"]
    assert summary["status"] == "error" and "nothing to summarize" in summary["error"]
    assert "summarize" not in llm.calls


def test_a_record_stored_without_its_authors_gets_them_from_its_source(tmp_path):
    pipeline, store, llm, project_id = run_all(tmp_path)
    papers = {p["ext_id"]: p for p in store.papers(project_id)}
    trial = papers["11111111"]
    assert trial["author_list"][0] == {"name": "Jisoo Kim", "affiliations": ["Department of Psychiatry, Seoul National University, Seoul, Korea."]}
    # Stored before authors were kept: fetched from PubMed when asked for, then kept.
    store.update_paper(trial["id"], author_list=None)
    assert pipeline.author_list(trial["id"])[1]["name"] == "Sun Lee"
    assert store.paper(trial["id"])["author_list"][1]["name"] == "Sun Lee"
    registry = papers["NCT01234567"]
    store.update_paper(registry["id"], author_list=None)
    assert pipeline.author_list(registry["id"]) == [{"name": "Jane Doe, MD", "affiliations": ["University of Example"], "role": "Principal investigator"}]


def test_the_summary_keeps_up_with_new_extractions_and_new_searches(tmp_path):
    from esf.evidence import decisions_basis

    pipeline, store, llm, project_id = run_all(tmp_path)
    pipeline.summarize(project_id)
    first = store.project(project_id)["summary"]
    assert first["status"] == "done" and llm.calls.count("overlap") == 1
    # Re-extracting a paper changes its numbers: the summary's basis no longer matches.
    paper = next(p for p in store.papers(project_id) if p["ext_id"] == "22222222")
    extraction = paper["extraction"]
    extraction["results"][0]["values"][0]["value"] += 1
    store.update_paper(paper["id"], extraction=extraction)
    assert decisions_basis(store.papers(project_id), store.decisions(project_id)) != first["basis"]
    pipeline.summarize(project_id)
    assert llm.calls.count("overlap") == 1  # what the overlap check sees (arms, sizes, outcomes) is the same
    # When the arms or their sizes change, the overlap check runs again before the next summary.
    extraction["groups"] = [*extraction["groups"], "Booster arm (n = 12)"]
    store.update_paper(paper["id"], extraction=extraction)
    pipeline.summarize(project_id)
    assert llm.calls.count("overlap") == 2
    # A failed rewrite keeps the summary there was, with the error.
    def broken(*args, **kwargs):
        raise RuntimeError("overloaded")
    llm.summarize = broken
    pipeline.summarize(project_id)
    kept = store.project(project_id)["summary"]
    assert kept["status"] == "done" and "overloaded" in kept["last_error"] and kept["result"] == first["result"]
    # A new search starts clean: no overlap, summary or progress from the one before.
    store.update_project(project_id, status="spec_ready")
    pipeline.store.update_project = (lambda original: (lambda pid, **f: (seen.append(set(f)), original(pid, **f))[1]))(store.update_project)
    seen: list = []
    pipeline.run(project_id)
    assert {"overlap", "summary", "progress"} <= next(f for f in seen if "search_plan" in f)
    assert store.project(project_id)["summary"] is None


def test_a_restart_keeps_the_summary_that_was_being_rewritten(tmp_path):
    from esf.store import Store

    pipeline, store, llm, project_id = run_all(tmp_path)
    pipeline.summarize(project_id)
    done = store.project(project_id)["summary"]
    store.update_project(project_id, summary={"status": "running", "previous": done})
    store.interrupt_running()
    kept = store.project(project_id)["summary"]
    assert kept["status"] == "done" and kept["result"] == done["result"] and "restarted" in kept["last_error"]
    assert isinstance(store, Store)


def out_of_credit() -> anthropic.BadRequestError:
    """What the Anthropic API answers once the account has no credit left."""
    message = "Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing to upgrade or purchase credits."
    body = {"type": "error", "error": {"type": "invalid_request_error", "message": message}}
    response = httpx.Response(400, json=body, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
    return anthropic.BadRequestError(message, response=response, body=body)


def test_an_account_out_of_credit_stops_the_run_and_the_papers_wait(tmp_path):
    pipeline, store, llm = make_pipeline(tmp_path, worker_concurrency=1)
    extract = llm.extract

    def until_the_credit_runs_out(spec, document, sink=None):
        if llm.calls.count("extract") >= 2:
            llm.calls.append("extract")
            raise out_of_credit()
        return extract(spec, document, sink)

    llm.extract = until_the_credit_runs_out
    project_id = store.create_project("We plan a randomized trial of a mindfulness app versus waitlist for PHQ-9 at 12 weeks.")
    pipeline.read_spec(project_id)
    pipeline.run(project_id)
    project = store.project(project_id)
    assert project["status"] == "error" and "run out of credit" in project["error"]
    assert llm.calls.count("extract") == 3  # one refused call, then no more
    papers = store.papers(project_id)
    assert [p["status"] for p in papers].count("extracted") == 2 and "error" not in [p["status"] for p in papers]
    # The paper the refusal came on waits with the others, saying why; Extract takes it up once there is credit.
    refused = next(p for p in papers if p["error"])
    assert refused["status"] == "screened_in" and refused["error"].startswith("Not extracted: The Anthropic account has run out of credit")
    llm.extract = extract
    spec = SearchSpec.model_validate(upgrade_spec(project["spec"]))
    pipeline.extract_paper(project_id, spec, refused["id"])
    assert store.paper(refused["id"])["status"] == "extracted"


def test_a_stopping_error_in_the_review_keeps_the_workers_extraction(tmp_path):
    pipeline, store, llm = make_pipeline(tmp_path, worker_concurrency=1)

    def refused(*args, **kwargs):
        llm.calls.append("adjudicate")
        raise out_of_credit()

    llm.adjudicate = refused
    project_id = store.create_project("We plan a randomized trial of a mindfulness app versus waitlist for PHQ-9 at 12 weeks.")
    pipeline.read_spec(project_id)
    pipeline.run(project_id)
    project = store.project(project_id)
    assert project["status"] == "error" and "run out of credit" in project["error"]
    pilot = next(p for p in store.papers(project_id) if p["ext_id"] == "44444444")
    # The worker's extraction stands, flagged, with the reason the manager did not review it.
    assert pilot["status"] == "extracted" and pilot["extracted_by"] == "worker"
    assert any(r.startswith("manager review failed: The Anthropic account has run out of credit") for r in pilot["escalation"]["reasons"])
    assert llm.calls.count("adjudicate") == 1


def test_a_failed_reading_of_the_context_keeps_the_extraction_and_can_be_read_again(tmp_path):
    pipeline, store, llm = make_pipeline(tmp_path)
    study_context = llm.study_context

    def overloaded(*args, **kwargs):
        llm.calls.append("context")
        raise RuntimeError("overloaded")

    llm.study_context = overloaded
    project_id = store.create_project("We plan a randomized trial of a mindfulness app versus waitlist for PHQ-9 at 12 weeks.")
    pipeline.read_spec(project_id)
    pipeline.run(project_id)
    project = store.project(project_id)
    assert project["status"] == "done", project["error"]
    extracted = store.papers(project_id, status="extracted")
    assert len(extracted) == 8 and llm.calls.count("context") == 8
    assert all(p["extraction"]["results"] and p["context"] == {"status": "error", "error": "overloaded"} for p in extracted)
    assert project["overlap"]["report"]["groups"]  # the run went on to the overlap check

    # Read again on request: the document is obtained again as extraction obtains it.
    llm.study_context = study_context
    trial = next(p for p in extracted if p["ext_id"] == "11111111")
    pipeline.read_context(project_id, trial["id"])
    context = store.paper(trial["id"])["context"]
    assert context["status"] == "done" and context["checks"] == {"sample_size": None, "results": ["exact"]}
    assert context["result"]["results"][0]["quote"] == "Analyses followed the intention-to-treat principle."
    assert store.paper(trial["id"])["extraction"] == trial["extraction"] and llm.calls.count("context") == 9
    # A paper extracted before the manager read contexts (an older project) is read the same way.
    pdf = next(p for p in extracted if p["ext_id"] == "22222222")
    store.update_paper(pdf["id"], context=None)
    pipeline.read_context(project_id, pdf["id"])
    assert store.paper(pdf["id"])["context"]["status"] == "done"
    # Nothing to read in a paper that is not extracted, or whose extraction has no results.
    excluded = next(p for p in store.papers(project_id) if p["status"] == "screened_out")
    pipeline.read_context(project_id, excluded["id"])
    assert store.paper(excluded["id"])["context"] is None
    store.update_paper(pdf["id"], extraction={**pdf["extraction"], "results": []})
    pipeline.read_context(project_id, pdf["id"])
    assert store.paper(pdf["id"])["context"] is None and llm.calls.count("context") == 10


def test_reading_the_context_again_says_when_the_text_cannot_be_had(tmp_path):
    pipeline, store, llm, project_id = run_all(tmp_path)
    trial = next(p for p in store.papers(project_id) if p["ext_id"] == "11111111")
    pipeline.sources = Sources(pipeline.settings, client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(404))))
    store.update_paper(trial["id"], abstract=None, doi=None)
    pipeline.read_context(project_id, trial["id"])
    context = store.paper(trial["id"])["context"]
    assert context["status"] == "error" and context["error"].startswith("Could not get the text")
    assert llm.calls.count("context") == 8


def test_a_stopping_error_in_the_reading_keeps_the_extraction_and_stops_the_run(tmp_path):
    pipeline, store, llm = make_pipeline(tmp_path, worker_concurrency=1)

    def refused(*args, **kwargs):
        llm.calls.append("context")
        raise out_of_credit()

    llm.study_context = refused
    project_id = store.create_project("We plan a randomized trial of a mindfulness app versus waitlist for PHQ-9 at 12 weeks.")
    pipeline.read_spec(project_id)
    pipeline.run(project_id)
    project = store.project(project_id)
    assert project["status"] == "error" and "run out of credit" in project["error"]
    assert llm.calls.count("context") == 1 and llm.calls.count("extract") == 1  # one refused call, then no more
    papers = store.papers(project_id)
    # The paper the refusal came on keeps its extraction, with the error; the others wait for the next run.
    [extracted] = [p for p in papers if p["status"] == "extracted"]
    assert extracted["extraction"]["results"] and extracted["context"]["status"] == "error"
    assert extracted["context"]["error"].startswith("The Anthropic account has run out of credit")
    assert {p["status"] for p in papers if p is not extracted} <= {"screened_in", "unsure", "screened_out"}


def test_an_extraction_without_results_is_not_read(tmp_path):
    pipeline, store, llm = make_pipeline(tmp_path)
    extract = llm.extract
    llm.extract = lambda spec, document, sink=None: extract(spec, document, sink).model_copy(update={"results": []})
    project_id = store.create_project("We plan a randomized trial of a mindfulness app versus waitlist for PHQ-9 at 12 weeks.")
    pipeline.read_spec(project_id)
    pipeline.run(project_id)
    extracted = store.papers(project_id, status="extracted")
    assert extracted and all(p["context"] is None for p in extracted)
    assert "context" not in llm.calls


def test_extracting_a_paper_again_clears_its_context_until_it_is_read_again(tmp_path):
    pipeline, store, llm, project_id = run_all(tmp_path)
    trial = next(p for p in store.papers(project_id) if p["ext_id"] == "11111111")
    seen = []
    study_context = llm.study_context

    def reading(spec, document, extraction, sink=None):
        seen.append(store.paper(trial["id"])["context"])  # while the manager reads it
        return study_context(spec, document, extraction, sink)

    llm.study_context = reading
    updates = []
    update_paper = store.update_paper
    store.update_paper = lambda paper_id, **fields: (updates.append(fields), update_paper(paper_id, **fields))[1]
    spec = SearchSpec.model_validate(upgrade_spec(store.project(project_id)["spec"]))
    pipeline.extract_paper(project_id, spec, trial["id"])
    assert updates[0] == {"status": "fetching", "error": None, "context": None}
    assert seen == [{"status": "running"}]
    assert store.paper(trial["id"])["status"] == "extracted" and store.paper(trial["id"])["context"]["status"] == "done"


def test_a_restart_ends_a_reading_that_was_under_way(tmp_path):
    _, store, _, project_id = run_all(tmp_path)
    trial = next(p for p in store.papers(project_id) if p["ext_id"] == "11111111")
    store.update_paper(trial["id"], context={"status": "running"})
    store.interrupt_running()
    paper = store.paper(trial["id"])
    assert paper["status"] == "extracted" and paper["context"]["status"] == "error" and "restarted" in paper["context"]["error"]
    assert all(p["context"]["status"] == "done" for p in store.papers(project_id, status="extracted") if p["id"] != trial["id"])


@pytest.mark.parametrize("code, stops", [
    ("subscription_sharing_unsupported_capability", True),
    ("subscription_sharing_v2_unsupported_capability", True),
    ("subscription_sharing_route_not_supported", True),
    ("subscription_sharing_v2_route_not_supported", True),
    ("subscription_sharing_usage_unavailable", False),
    ("invalid_value", False),
])
def test_a_kind_of_request_the_plan_refuses_stops_the_run(code, stops):
    assert stops_the_run(OpenAIError(code, "refused", 400)) is stops
    # A sign-in ChatGPT won't let the app renew for longer than a job waits is; ChatGPT out of reach for a moment is not.
    assert stops_the_run(ChatGPTError("refresh_not_ready", "Not yet.", retryable=True)) is True
    assert stops_the_run(ChatGPTError("network_error", "Offline.", retryable=True)) is False
    assert stops_the_run(ChatGPTError("reauth_required", "Sign in again.")) is True


def run_on_the_plan(tmp_path, refuse):
    """A run on OpenAI models whose Responses API answers 400 with the code refuse(body) gives, if any."""
    settings = Settings(data_dir=tmp_path, contact_email="test@example.com", audit_fraction=0.0, worker_concurrency=1)
    store = Store(tmp_path / "test.sqlite3")
    api = FakeOpenAI()
    sent = []

    def handler(request):
        body = json.loads(request.content)
        sent.append(body)
        code = refuse(body)
        if code:
            return httpx.Response(400, json={"error": {"code": code, "message": "Not on the plan."}})
        return api(request)

    llm = OpenAILLM(settings, lambda: "plan-token", client=httpx.Client(transport=httpx.MockTransport(handler)))
    llm.responses.sleep = lambda seconds: None
    pipeline = Pipeline(store, llm, Sources(settings, client=mock_client(LiteratureAPIs())), settings)
    project_id = store.create_project("We plan a randomized trial of a mindfulness app versus waitlist for PHQ-9 at 12 weeks.")
    pipeline.read_spec(project_id)
    pipeline.run(project_id)
    return store, project_id, sent


def step(body: dict) -> str | None:
    return body.get("text", {}).get("format", {}).get("name")


@pytest.mark.parametrize("code", ["subscription_sharing_unsupported_capability", "subscription_sharing_v2_route_not_supported"])
def test_a_screening_request_the_plan_refuses_stops_the_run_at_once(tmp_path, code):
    store, project_id, sent = run_on_the_plan(tmp_path, lambda body: code if step(body) == "ScreenDecision" else None)
    project = store.project(project_id)
    assert project["status"] == "error" and "ChatGPT plan usage does not support" in project["error"]
    assert [step(body) for body in sent].count("ScreenDecision") == 1  # one refused request, then no more


def test_a_pdf_the_plan_refuses_goes_as_its_text_layer_and_the_run_goes_on(tmp_path):
    def no_files(body):
        parts = [part for item in body["input"] if isinstance(item.get("content"), list) for part in item["content"]]
        return "subscription_sharing_unsupported_capability" if any(part.get("type") == "input_file" for part in parts) else None

    store, project_id, sent = run_on_the_plan(tmp_path, no_files)
    project = store.project(project_id)
    assert project["status"] == "done", project["error"]
    assert sum(no_files(body) is not None for body in sent) == 1  # refused once, then sent as text
    texts = [part["text"] for body in sent if step(body) == "PaperExtraction" for part in body["input"][0]["content"]
             if part["type"] == "input_text"]
    assert any(text.startswith("Document: Brief mindfulness") and "(the text layer of its PDF)" in text for text in texts)
    assert sum(p["status"] == "extracted" for p in store.papers(project_id)) == 8


@pytest.mark.parametrize("late", [300, JOB_REFRESH_WAIT + 300])
def test_a_renewal_chatgpt_allows_late_is_waited_for_or_stops_the_run(tmp_path, late):
    # The access token runs out during screening, and ChatGPT allows its renewal only `late` seconds on.
    settings = Settings(data_dir=tmp_path, contact_email="test@example.com", audit_fraction=0.0, worker_concurrency=1)
    store = Store(tmp_path / "test.sqlite3")
    session, sign_in, clock = new_session(tmp_path / "plan")
    issued = clock.now
    sign_in.token_changes = {"earliest_refresh_at": issued + 3600 + late}
    session.finish(sign_in.authorize(session.start(PORT)), HOST)
    responses, screened, tokens = FakeOpenAI(), [], []

    def handler(request):
        body = json.loads(request.content)
        tokens.append(request.headers["authorization"])
        if step(body) == "ScreenDecision":
            screened.append(body)
            if len(screened) == 2:
                clock.now = issued + 3600  # after this record, the token has expired
        return responses(request)

    llm = plan_llm(settings, session)  # as the server makes it for a job on the plan
    llm.responses.client = httpx.Client(transport=httpx.MockTransport(handler))
    pipeline = Pipeline(store, llm, Sources(settings, client=mock_client(LiteratureAPIs())), settings)
    project_id = store.create_project("We plan a randomized trial of a mindfulness app versus waitlist for PHQ-9 at 12 weeks.")
    pipeline.read_spec(project_id)
    pipeline.run(project_id)
    project, papers = store.project(project_id), store.papers(project_id)
    assert not any("Screening failed" in (p["screen"] or {}).get("reason", "") for p in papers)
    if late <= JOB_REFRESH_WAIT:
        # The job waited (on the session's clock) for the renewal, and went on with the new token.
        assert project["status"] == "done", project["error"]
        assert clock.slept == [late] and sign_in.refreshes == 1 and tokens[-1] == "Bearer at-2"
        assert len(screened) == 9 and session.state()["error"] is None
    else:
        # Too long a wait: the run stops at once, saying why, and the records not yet screened wait for the next run.
        assert project["status"] == "error" and project["error"] == REFRESH_NOT_READY
        assert clock.slept == [] and sign_in.refreshes == 0 and len(screened) == 2
        assert [p["status"] for p in papers].count("candidate") == len(papers) - 2
        assert session.state()["status"] == "connected" and session.state()["error"] == REFRESH_NOT_READY
