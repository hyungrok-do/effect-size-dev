from esf.config import Settings
from esf.fulltext import get_fulltext
from esf.jats import jats_to_text
from esf.sources import Sources, europepmc_pmid, europepmc_record, europepmc_url, parse_pubmed_xml, trial_record, trial_results_text
from fixtures import (
    CITED_TRIAL, EUROPEPMC, JATS, NOT_A_STUDY, PDF_LINE, PMC_ONLY, PREPRINT, PUBMED_XML, REVIEW, TRIAL, LiteratureAPIs, mock_client,
)


def sources(email="test@example.com"):
    handler = LiteratureAPIs()
    return Sources(Settings(contact_email=email), client=mock_client(handler)), handler


def test_parse_pubmed_xml():
    records = {r.pmid: r for r in parse_pubmed_xml(PUBMED_XML)}
    first = records["11111111"]
    assert first.title.startswith("A mindfulness app")
    assert first.year == "2021"
    assert first.doi == "10.1000/rct1"
    assert first.pmcid == "PMC9000001"
    assert first.nct_ids == ["NCT01234567"]
    assert "Randomized Controlled Trial" in first.pubtypes
    assert first.abstract.splitlines() == [
        "BACKGROUND: Depression is common among students.",
        "RESULTS: At 12 weeks PHQ-9 was lower with the app (8.1 vs 10.3).",
    ]
    assert first.authors == "Kim J, Lee S, Park H, et al."
    assert records["22222222"].authors == "Mindful Students Trial Group"
    assert records["33333333"].authors == ""
    assert records["22222222"].year == "2019"  # from <MedlineDate>
    assert records["22222222"].pmcid is None


def test_jats_keeps_tables_as_rows_and_drops_references():
    text = jats_to_text(JATS)
    assert text.startswith("# A mindfulness app")
    assert "## Results" in text
    assert "[Table 2] Outcomes at 12 weeks, mean (SD)" in text
    assert "Outcome | App (n = 100) | Waitlist (n = 100) | Difference (95% CI)" in text
    assert "PHQ-9, 12 weeks | 8.1 (4.2) | 10.3 (4.5) | −2.2 (−3.4 to −1.0)" in text
    assert "Table note: Means adjusted for baseline." in text
    assert "[Figure 1] CONSORT flow diagram. (figure image not included)" in text
    assert "Of 200 students randomized, 98 and 97 completed follow-up (Table 2)." in text
    assert "should not appear" not in text


def test_trial_results_text_keeps_labels_with_numbers():
    record = trial_record(TRIAL)
    assert record.ext_id == "NCT01234567"
    assert record.year == "2019"
    assert "Result publications (PMID): 11111111" in record.abstract
    text = trial_results_text(TRIAL)
    assert "## Outcome measure (primary): PHQ-9 Total Score" in text
    assert "Analyzed (participants): Mindfulness App: 98 | Waitlist: 97" in text
    assert "Values: Mindfulness App: 8.1 (SD 4.2) | Waitlist: 10.3 (SD 4.5)" in text
    assert "Analysis (Mindfulness App vs Waitlist); Mean Difference (Final Values) -2.2; 95% CI -3.4 to -1.0; p = <0.001; method: ANCOVA" in text


def test_pubmed_client_sends_identification():
    src, handler = sources()
    count, ids = src.pubmed.search("mindfulness", 10)
    assert count == 4 and ids[0] == "11111111"
    body = handler.requests[-1].content.decode()
    assert "tool=effect-size-finder" in body and "email=test%40example.com" in body
    assert [r.pmid for r in src.pubmed.fetch(ids)] == ids


def test_fulltext_chain():
    src, _ = sources()
    by_pmid = {r.pmid: r.__dict__ for r in parse_pubmed_xml(PUBMED_XML)}
    pmc = get_fulltext(by_pmid["11111111"], src)
    assert pmc.kind == "pmc_xml" and "8.1 (4.2)" in pmc.text
    pdf = get_fulltext(by_pmid["22222222"], src)
    assert pdf.kind == "oa_pdf" and pdf.pdf.startswith(b"%PDF")
    assert PDF_LINE in pdf.text
    assert pdf.document_block("t")["source"]["media_type"] == "application/pdf"
    abstract = get_fulltext(by_pmid["44444444"], src)
    assert abstract.kind == "abstract" and "7.9 (SD 3.8" in abstract.text
    trial = get_fulltext({"source": "clinicaltrials", "ext_id": "NCT01234567"}, src)
    assert trial.kind == "registry" and "Waitlist: 10.3 (SD 4.5)" in trial.text


def test_unpaywall_needs_an_email():
    src, handler = sources(email=None)
    assert src.unpaywall.pdf_url("10.1000/rct2") is None
    assert not handler.requests


def test_references_come_from_europe_pmc_then_pubmed_central():
    src, handler = sources()
    # Europe PMC's list, PubMed records only, in the order cited.
    assert src.references(REVIEW) == [CITED_TRIAL, "11111111", NOT_A_STUDY]
    assert not any(r.url.path.endswith("elink.fcgi") for r in handler.requests)
    # Europe PMC has no list for this one (404), so PubMed Central's is used.
    assert src.references("88888888") == [CITED_TRIAL]
    elink = handler.requests[-1]
    assert elink.url.path.endswith("elink.fcgi") and b"linkname=pubmed_pubmed_refs" in elink.content
    assert src.references("99999999") == []


def test_counting_asks_pubmed_for_no_records():
    src, handler = sources()
    assert src.pubmed.search("mindfulness AND (systematic review[pt])", 0) == (1, [])
    assert b"retmax=0" in handler.requests[-1].content


def test_europe_pmc_search_keeps_papers_and_preprints():
    src, handler = sources()
    total, results = src.europepmc.search('mindfulness AND METHODS:"PHQ-9"', 100)
    assert total == 4 and [r["id"] for r in results] == ["22222222", PMC_ONLY, PREPRINT]  # not the patent
    assert handler.requests[-1].url.params["pageSize"] == "100"
    assert handler.requests[-1].url.params["resultType"] == "core"
    assert [europepmc_pmid(r) for r in results] == ["22222222", None, None]
    record = europepmc_record(results[1])
    assert (record.source, record.ext_id, record.pmcid, record.journal) == ("europepmc", PMC_ONLY, PMC_ONLY, "Nursing Education Trials")
    assert record.title == "Mindfulness app use and depressive symptoms in nursing students: a randomised trial"
    assert record.authors == "Ahmed N, Brown T, Clark R, et al."
    assert europepmc_record(results[2]).pubtypes == ["Preprint"]
    assert europepmc_url(PREPRINT) == f"https://europepmc.org/article/PPR/{PREPRINT}"
    assert EUROPEPMC["resultList"]["result"][3]["source"] == "PAT"
