"""Responses in the shape the literature APIs return, served by an httpx mock transport, and a fake
Claude that stands in for the models in pipeline and server tests."""

from __future__ import annotations

import json
import re
from urllib.parse import parse_qs

import httpx

from esf import prompts
from esf.schemas import (
    Comparator,
    Criterion,
    EvidenceSummary,
    Intervention,
    NeededStatistic,
    NeedSummary,
    PlanningRecommendation,
    Outcome,
    OverlapGroup,
    OverlapReport,
    OutcomeResult,
    PaperExtraction,
    Population,
    ResultContext,
    ReportedValue,
    ScreenDecision,
    SearchLevel,
    SearchPlan,
    SearchSpec,
    StudyContext,
    TermGroup,
)

CONTEXT_INPUT = prompts.CONTEXT_INPUT

PUBMED_XML = """<?xml version="1.0" ?>
<!DOCTYPE PubmedArticleSet PUBLIC "-//NLM//DTD PubMedArticle, 1st January 2025//EN" "https://dtd.nlm.nih.gov/ncbi/pubmed/out/pubmed_250101.dtd">
<PubmedArticleSet>
<PubmedArticle>
  <MedlineCitation Status="MEDLINE" Owner="NLM">
    <PMID Version="1">11111111</PMID>
    <Article PubModel="Print-Electronic">
      <Journal><JournalIssue CitedMedium="Internet"><PubDate><Year>2021</Year><Month>Mar</Month></PubDate></JournalIssue>
        <Title>Journal of Student Mental Health</Title><ISOAbbreviation>J Stud Ment Health</ISOAbbreviation></Journal>
      <ArticleTitle>A mindfulness app for depressive symptoms in university students: a randomized controlled trial.</ArticleTitle>
      <AuthorList CompleteYN="Y"><Author><LastName>Kim</LastName><ForeName>Jisoo</ForeName><Initials>J</Initials>
        <AffiliationInfo><Affiliation>Department of Psychiatry, Seoul National
          University, Seoul, Korea.</Affiliation></AffiliationInfo></Author>
        <Author><LastName>Lee</LastName><ForeName>Sun</ForeName><Initials>S</Initials>
        <AffiliationInfo><Affiliation>College of Nursing, Yonsei University, Seoul, Korea.</Affiliation></AffiliationInfo>
        <AffiliationInfo><Affiliation>Mind Health Institute, Seoul, Korea.</Affiliation></AffiliationInfo></Author>
        <Author><LastName>Park</LastName><Initials>H</Initials><AffiliationInfo><Affiliation>1Department of Statistics,
          Example University, Seoul, Korea; 2Biostatistics Unit, Example Hospital, Seoul, Korea. Electronic address: hpark@example.ac.kr.</Affiliation></AffiliationInfo>
          <AffiliationInfo><Affiliation>Department of Statistics, Example University, Seoul, Korea</Affiliation></AffiliationInfo></Author><Author><LastName>Choi</LastName><Initials>Y</Initials></Author></AuthorList>
      <ELocationID EIdType="doi" ValidYN="Y">10.1000/rct1</ELocationID>
      <Abstract>
        <AbstractText Label="BACKGROUND" NlmCategory="BACKGROUND">Depression is common among students.</AbstractText>
        <AbstractText Label="RESULTS" NlmCategory="RESULTS">At 12 weeks PHQ-9 was lower with the app (8.1 vs 10.3).</AbstractText>
      </Abstract>
      <DataBankList CompleteYN="Y"><DataBank><DataBankName>ClinicalTrials.gov</DataBankName>
        <AccessionNumberList><AccessionNumber>NCT01234567</AccessionNumber></AccessionNumberList></DataBank></DataBankList>
      <PublicationTypeList><PublicationType UI="D016449">Randomized Controlled Trial</PublicationType>
        <PublicationType UI="D016428">Journal Article</PublicationType></PublicationTypeList>
    </Article>
  </MedlineCitation>
  <PubmedData><ArticleIdList><ArticleId IdType="pubmed">11111111</ArticleId><ArticleId IdType="doi">10.1000/rct1</ArticleId>
    <ArticleId IdType="pmc">PMC9000001</ArticleId></ArticleIdList></PubmedData>
</PubmedArticle>
<PubmedArticle>
  <MedlineCitation Status="MEDLINE" Owner="NLM">
    <PMID Version="1">22222222</PMID>
    <Article PubModel="Print">
      <Journal><JournalIssue CitedMedium="Print"><PubDate><MedlineDate>2019 Nov-Dec</MedlineDate></PubDate></JournalIssue>
        <Title>Trials in Psychiatry</Title></Journal>
      <ArticleTitle>Brief mindfulness training versus waitlist: a randomised trial.</ArticleTitle>
      <AuthorList><Author><CollectiveName>Mindful Students Trial Group</CollectiveName></Author></AuthorList>
      <Abstract><AbstractText>We randomized 120 students to mindfulness or waitlist.</AbstractText></Abstract>
      <PublicationTypeList><PublicationType UI="D016449">Randomized Controlled Trial</PublicationType></PublicationTypeList>
    </Article>
  </MedlineCitation>
  <PubmedData><ArticleIdList><ArticleId IdType="pubmed">22222222</ArticleId><ArticleId IdType="doi">10.1000/rct2</ArticleId></ArticleIdList></PubmedData>
</PubmedArticle>
<PubmedArticle>
  <MedlineCitation Status="MEDLINE" Owner="NLM">
    <PMID Version="1">33333333</PMID>
    <Article PubModel="Print">
      <Journal><JournalIssue><PubDate><Year>2020</Year></PubDate></JournalIssue><Title>Neuroscience Letters</Title></Journal>
      <ArticleTitle>Stress responses in a mouse model of depression.</ArticleTitle>
      <Abstract><AbstractText>Mice were exposed to chronic stress.</AbstractText></Abstract>
      <PublicationTypeList><PublicationType UI="D016428">Journal Article</PublicationType></PublicationTypeList>
    </Article>
  </MedlineCitation>
  <PubmedData><ArticleIdList><ArticleId IdType="pubmed">33333333</ArticleId></ArticleIdList></PubmedData>
</PubmedArticle>
<PubmedArticle>
  <MedlineCitation Status="MEDLINE" Owner="NLM">
    <PMID Version="1">44444444</PMID>
    <Article PubModel="Print">
      <Journal><JournalIssue><PubDate><Year>2018</Year></PubDate></JournalIssue><Title>Student Health Reports</Title></Journal>
      <ArticleTitle>Mindfulness groups and depressive symptoms: a pilot randomized trial.</ArticleTitle>
      <Abstract><AbstractText Label="RESULTS">PHQ-9 at 12 weeks: mindfulness 7.9 (SD 3.8, n = 20); control 9.6 (SD 4.1, n = 21).</AbstractText></Abstract>
      <PublicationTypeList><PublicationType UI="D016449">Randomized Controlled Trial</PublicationType></PublicationTypeList>
    </Article>
  </MedlineCitation>
  <PubmedData><ArticleIdList><ArticleId IdType="pubmed">44444444</ArticleId></ArticleIdList></PubmedData>
</PubmedArticle>
<PubmedArticle>
  <MedlineCitation Status="MEDLINE" Owner="NLM">
    <PMID Version="1">55555555</PMID>
    <Article PubModel="Print">
      <Journal><JournalIssue><PubDate><Year>2022</Year></PubDate></JournalIssue><Title>Digital Mental Health Reviews</Title></Journal>
      <ArticleTitle>Mindfulness apps for depressive symptoms: a systematic review and meta-analysis.</ArticleTitle>
      <AuthorList><Author ValidYN="N"><LastName>Garsia</LastName><Initials>M</Initials></Author>
        <Author><LastName>Garcia</LastName><Initials>M</Initials></Author><Author><LastName>Chen</LastName><Initials>L</Initials></Author></AuthorList>
      <Abstract><AbstractText Label="RESULTS">Twelve trials were included. Apps reduced depressive symptoms (pooled SMD 0.35, 95% CI 0.20 to 0.50).</AbstractText></Abstract>
      <PublicationTypeList><PublicationType UI="D000078182">Systematic Review</PublicationType>
        <PublicationType UI="D017418">Meta-Analysis</PublicationType></PublicationTypeList>
    </Article>
  </MedlineCitation>
  <PubmedData><ArticleIdList><ArticleId IdType="pubmed">55555555</ArticleId></ArticleIdList></PubmedData>
</PubmedArticle>
<PubmedArticle>
  <MedlineCitation Status="MEDLINE" Owner="NLM">
    <PMID Version="1">66666666</PMID>
    <Article PubModel="Print">
      <Journal><JournalIssue><PubDate><Year>2020</Year></PubDate></JournalIssue><Title>Campus Health Trials</Title></Journal>
      <ArticleTitle>Smartphone mindfulness for student depression: a randomized controlled trial.</ArticleTitle>
      <Abstract><AbstractText Label="RESULTS">PHQ-9 at 12 weeks: app 8.4 (SD 4.0, n = 75); waitlist 10.1 (SD 4.3, n = 74).</AbstractText></Abstract>
      <PublicationTypeList><PublicationType UI="D016449">Randomized Controlled Trial</PublicationType></PublicationTypeList>
    </Article>
  </MedlineCitation>
  <PubmedData><ArticleIdList><ArticleId IdType="pubmed">66666666</ArticleId></ArticleIdList></PubmedData>
</PubmedArticle>
<PubmedArticle>
  <MedlineCitation Status="MEDLINE" Owner="NLM">
    <PMID Version="1">77777777</PMID>
    <Article PubModel="Print">
      <Journal><JournalIssue><PubDate><Year>2015</Year></PubDate></JournalIssue><Title>Methods Notes</Title></Journal>
      <ArticleTitle>Pooling continuous outcomes: a narrative review of methods.</ArticleTitle>
      <PublicationTypeList><PublicationType UI="D016428">Journal Article</PublicationType>
        <PublicationType UI="D016454">Review</PublicationType></PublicationTypeList>
    </Article>
  </MedlineCitation>
  <PubmedData><ArticleIdList><ArticleId IdType="pubmed">77777777</ArticleId></ArticleIdList></PubmedData>
</PubmedArticle>
</PubmedArticleSet>
"""

# What the primary level's query finds; the reviews level finds REVIEW, which cites CITED_TRIAL (found
# only through the review), one of PMIDS, and NOT_A_STUDY (a narrative review, which is dropped).
PMIDS = ["11111111", "22222222", "33333333", "44444444"]
REVIEW, CITED_TRIAL, NOT_A_STUDY = "55555555", "66666666", "77777777"
ALL_PMIDS = [*PMIDS, REVIEW, CITED_TRIAL, NOT_A_STUDY]

# What the full-text search in Europe PMC finds: a paper PubMed already found, one only in PubMed
# Central, a preprint, and a patent (which is left out).
PMC_ONLY, PREPRINT = "PMC9000002", "PPR555555"
PMC_ONLY_LINE = "PHQ-9 at 8 weeks: app 9.2 (SD 4.1, n = 40); control 10.8 (SD 4.4, n = 41)."
PREPRINT_LINE = "PHQ-9 at 12 weeks was 7.5 (SD 3.9) with the app and 9.1 (SD 4.2) on the waitlist, n = 30 per arm."
EUROPEPMC = {
    "version": "6.9",
    "hitCount": 4,
    "resultList": {"result": [
        {"id": "22222222", "source": "MED", "pmid": "22222222", "title": "Brief mindfulness training versus waitlist: a randomised trial.",
         "pubYear": "2019", "pubType": "research-article; journal article"},
        {"id": PMC_ONLY, "source": "PMC", "pmcid": PMC_ONLY,
         "title": "Mindfulness app use and depressive symptoms in <i>nursing</i> students: a randomised trial",
         "authorString": "Ahmed N, Brown T, Clark R, Davis P.",
         "authorList": {"author": [
             {"fullName": "Ahmed N", "firstName": "Nadia", "lastName": "Ahmed", "initials": "N",
              "authorAffiliationDetailsList": {"authorAffiliation": [{"affiliation": "School of Nursing, Example University, Leeds, UK."}]}},
             {"fullName": "Brown T", "firstName": "Tom", "lastName": "Brown", "initials": "T", "affiliation": "Leeds Teaching Hospitals, Leeds, UK."},
             {"fullName": "Clark R", "lastName": "Clark", "initials": "R"},
             {"fullName": "Davis P", "firstName": "Pat", "lastName": "Davis", "initials": "P"}]},
         "pubYear": "2023", "journalInfo": {"journal": {"title": "Nursing Education Trials"}},
         "pubTypeList": {"pubType": ["research-article", "Journal Article"]}, "pubType": "research-article; journal article",
         "abstractText": f"<h4>Results</h4><p>{PMC_ONLY_LINE}</p>"},
        {"id": PREPRINT, "source": "PPR", "doi": "10.1101/2024.01.01.24300001",
         "title": "A smartphone mindfulness programme for depressive symptoms: a pilot randomised trial",
         "pubYear": 2024, "bookOrReportDetails": {"publisher": "medRxiv"}, "pubTypeList": {"pubType": "Preprint"},
         "pubType": "preprint", "abstractText": PREPRINT_LINE},
        {"id": "EP1234567", "source": "PAT", "title": "A mindfulness device", "pubYear": "2020"},
    ]},
}
REFERENCES = {
    "hitCount": 5,
    "referenceList": {"reference": [
        {"id": CITED_TRIAL, "source": "MED", "title": "Smartphone mindfulness for student depression"},
        {"id": "11111111", "source": "MED", "title": "A mindfulness app for depressive symptoms"},
        {"id": NOT_A_STUDY, "source": "MED", "title": "Pooling continuous outcomes"},
        {"id": "PPR12345", "source": "PPR", "title": "A preprint"},
        {"title": "A book chapter with no identifier"},
    ]},
}

ESUMMARY = {
    "header": {"type": "esummary", "version": "0.3"},
    "result": {
        "uids": PMIDS,
        **{
            pmid: {
                "uid": pmid,
                "pubdate": "2021 Mar",
                "source": "J Example",
                "title": f"Title {pmid}",
                "pubtype": ["Journal Article"],
                "articleids": [{"idtype": "pubmed", "value": pmid}],
            }
            for pmid in PMIDS
        },
    },
}

JATS = """<?xml version="1.0" encoding="UTF-8"?>
<article xmlns:xlink="http://www.w3.org/1999/xlink" xmlns:mml="http://www.w3.org/1998/Math/MathML" article-type="research-article">
<front><article-meta>
  <title-group><article-title>A mindfulness app for depressive symptoms in university students</article-title></title-group>
  <abstract><sec><title>Results</title><p>PHQ-9 at 12 weeks was 8.1 in the app arm and 10.3 in the control arm.</p></sec></abstract>
</article-meta></front>
<body>
  <sec><title>Methods</title><p>Students were randomized 1:1 to the app or a waitlist. The primary outcome was the PHQ-9 at 12 weeks.
  Analyses followed the intention-to-treat principle.</p></sec>
  <sec><title>Results</title>
    <p>Of 200 students randomized, 98 and 97 completed follow-up (<xref ref-type="table" rid="t2">Table 2</xref>).
    <table-wrap id="t2"><label>Table 2</label><caption><p>Outcomes at 12 weeks, mean (SD)</p></caption>
      <table><thead><tr><th>Outcome</th><th>App (n = 100)</th><th>Waitlist (n = 100)</th><th>Difference (95% CI)</th></tr></thead>
      <tbody><tr><td>PHQ-9, 12 weeks</td><td>8.1 (4.2)</td><td>10.3 (4.5)</td><td>&#8722;2.2 (&#8722;3.4 to &#8722;1.0)</td></tr>
      <tr><td>GAD-7, 12 weeks</td><td>6.0 (3.9)</td><td>6.8 (4.0)</td><td>&#8722;0.8 (&#8722;1.9 to 0.3)</td></tr></tbody></table>
      <table-wrap-foot><fn><p>Means adjusted for baseline.</p></fn></table-wrap-foot>
    </table-wrap></p>
    <fig id="f1"><label>Figure 1</label><caption><p>CONSORT flow diagram.</p></caption></fig>
  </sec>
</body>
<back><ref-list><ref id="r1"><mixed-citation>Smith J. A reference that should not appear.</mixed-citation></ref></ref-list></back>
</article>
"""

TRIAL = {
    "protocolSection": {
        "identificationModule": {"nctId": "NCT01234567", "briefTitle": "Mindfulness App Trial", "officialTitle": "A Randomized Trial of a Mindfulness App for Student Depression"},
        "statusModule": {"overallStatus": "COMPLETED", "startDateStruct": {"date": "2019-09"}},
        "descriptionModule": {"briefSummary": "Tests a mindfulness app against a waitlist."},
        "conditionsModule": {"conditions": ["Depression"]},
        "designModule": {"studyType": "INTERVENTIONAL", "designInfo": {"allocation": "RANDOMIZED"}, "enrollmentInfo": {"count": 200}},
        "armsInterventionsModule": {"interventions": [{"name": "Mindfulness app"}]},
        "outcomesModule": {"primaryOutcomes": [{"measure": "PHQ-9", "timeFrame": "12 weeks"}]},
        "referencesModule": {"references": [{"pmid": "11111111", "type": "RESULT", "citation": "..."}]},
        "contactsLocationsModule": {"overallOfficials": [
            {"name": "Jane  Doe, MD", "affiliation": "University of Example", "role": "PRINCIPAL_INVESTIGATOR"}]},
    },
    "resultsSection": {
        "outcomeMeasuresModule": {
            "outcomeMeasures": [
                {
                    "type": "PRIMARY",
                    "title": "PHQ-9 Total Score",
                    "description": "Patient Health Questionnaire-9, range 0-27.",
                    "timeFrame": "12 weeks",
                    "paramType": "MEAN",
                    "dispersionType": "STANDARD_DEVIATION",
                    "unitOfMeasure": "score on a scale",
                    "groups": [
                        {"id": "OG000", "title": "Mindfulness App"},
                        {"id": "OG001", "title": "Waitlist"},
                    ],
                    "denoms": [{"units": "Participants", "counts": [{"groupId": "OG000", "value": "98"}, {"groupId": "OG001", "value": "97"}]}],
                    "classes": [
                        {"categories": [{"measurements": [
                            {"groupId": "OG000", "value": "8.1", "spread": "4.2"},
                            {"groupId": "OG001", "value": "10.3", "spread": "4.5"},
                        ]}]}
                    ],
                    "analyses": [
                        {
                            "groupIds": ["OG000", "OG001"],
                            "paramType": "Mean Difference (Final Values)",
                            "paramValue": "-2.2",
                            "ciPctValue": "95",
                            "ciLowerLimit": "-3.4",
                            "ciUpperLimit": "-1.0",
                            "pValue": "<0.001",
                            "statisticalMethod": "ANCOVA",
                        }
                    ],
                }
            ]
        }
    },
    "hasResults": True,
}

PDF_LINE = "PHQ-9 at 12 weeks (n = 60 per arm): 9.0 (4.0) vs 11.0 (4.4)"


def make_pdf(text: str) -> bytes:
    """A one-page PDF whose text layer is `text`."""
    content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


class LiteratureAPIs:
    """httpx mock transport handler for PubMed, ClinicalTrials.gov, Europe PMC, Unpaywall."""

    def __init__(self):
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = request.url
        if url.host == "eutils.ncbi.nlm.nih.gov":
            # The Python client POSTs a form; the browser edition sends a GET with a query string.
            form = {**dict(url.params), **{k: v[0] for k, v in parse_qs(request.content.decode()).items()}}
            if url.path.endswith("esearch.fcgi"):
                ids = [REVIEW] if "systematic review" in form["term"] or "meta-analy" in form["term"] else PMIDS
                shown = [] if form.get("retmax") == "0" else ids
                return httpx.Response(200, json={"esearchresult": {"count": str(len(ids)), "retmax": str(len(shown)), "idlist": shown}})
            if url.path.endswith("esummary.fcgi"):
                return httpx.Response(200, json=ESUMMARY)
            if url.path.endswith("efetch.fcgi"):
                wanted = set(form["id"].split(","))
                assert wanted <= set(ALL_PMIDS)
                # Like PubMed, return only the records asked for.
                kept = re.findall(r"<PubmedArticle>.*?</PubmedArticle>", PUBMED_XML, re.DOTALL)
                kept = [a for a in kept if re.search(r"<PMID Version=\"1\">(\d+)</PMID>", a).group(1) in wanted]
                body = PUBMED_XML.split("<PubmedArticle>")[0] + "".join(kept) + "\n</PubmedArticleSet>\n"
                return httpx.Response(200, text=body, headers={"content-type": "text/xml"})
            if url.path.endswith("elink.fcgi"):
                links = [{"dbto": "pubmed", "linkname": "pubmed_pubmed_refs", "links": [CITED_TRIAL]}] if form["id"] == "88888888" else []
                return httpx.Response(200, json={"linksets": [{"dbfrom": "pubmed", "ids": [form["id"]], "linksetdbs": links}]})
        if url.host == "clinicaltrials.gov":
            if url.path == "/api/v2/studies":
                return httpx.Response(200, json={"totalCount": 1, "studies": [TRIAL]})
            if url.path == "/api/v2/studies/NCT01234567":
                return httpx.Response(200, json=TRIAL)
        if url.host == "www.ebi.ac.uk" and url.path.endswith("/PMC9000001/fullTextXML"):
            return httpx.Response(200, text=JATS, headers={"content-type": "application/xml"})
        if url.host == "www.ebi.ac.uk" and url.path.endswith(f"/MED/{REVIEW}/references"):
            return httpx.Response(200, json=REFERENCES)
        if url.host == "www.ebi.ac.uk" and url.path == "/europepmc/webservices/rest/search":
            assert url.params["resultType"] in ("core", "lite") and url.params["format"] == "json"
            return httpx.Response(200, json=EUROPEPMC)
        if url.host == "api.unpaywall.org" and url.path == "/v2/10.1000/rct2":
            return httpx.Response(200, json={"doi": "10.1000/rct2", "is_oa": True, "best_oa_location": {"url_for_pdf": "https://example.org/rct2.pdf"}})
        if url.host == "example.org" and url.path == "/rct2.pdf":
            return httpx.Response(200, content=make_pdf(PDF_LINE), headers={"content-type": "application/pdf"})
        return httpx.Response(404, text="not found")


def mock_client(handler: LiteratureAPIs) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


SPEC = SearchSpec(
    question="Does a mindfulness app reduce PHQ-9 at 12 weeks versus waitlist in students?",
    design="parallel_rct",
    population=Population(description="University students", condition="", age="", setting="Universities"),
    intervention=Intervention(name="Mindfulness app", delivery="Smartphone app", dose="", duration="12 weeks"),
    comparator=Comparator(name="Waitlist", kind="waitlist"),
    outcomes=[Outcome(name="Depressive symptoms", measure="PHQ-9", role="primary", type="continuous", timepoint="12 weeks")],
    statistics_needed=[NeededStatistic(outcome="Depressive symptoms", statistics=["mean", "sd", "n"], per="each_arm",
                                       timepoint="12 weeks", purpose="The difference and its SD")],
    stated_values=[],
    inclusion_criteria=[Criterion(about="design", rule="Randomized trials in students")],
    exclusion_criteria=[Criterion(about="population", rule="Animal studies")],
    search_terms=[TermGroup(concept="intervention", terms=["mindfulness"]), TermGroup(concept="outcome", terms=["PHQ-9"])],
    open_questions=[],
)


def _value(group, statistic, value, quote, location="Table 2"):
    return ReportedValue(group=group, statistic=statistic, value=value, describes="PHQ-9 at 12 weeks", quote=quote, location=location)


def _result(values, confidence="high", concerns=()):
    return OutcomeResult(
        outcome_measure="PHQ-9",
        timepoint="12 weeks",
        analysis_population="itt",
        adjusted="adjusted",
        relevance="direct",
        values=values,
        confidence=confidence,
        concerns=list(concerns),
    )


def _extraction(results):
    return PaperExtraction(
        study_design="parallel RCT", population="students", groups=["App", "Control"],
        matches_spec=True, mismatch_reasons=[], results=results, notes="",
    )


ROW = "PHQ-9, 12 weeks | 8.1 (4.2) | 10.3 (4.5) | −2.2 (−3.4 to −1.0)"
HEADER = "Outcome | App (n = 100) | Waitlist (n = 100) | Difference (95% CI)"
ABSTRACT_LINE = "PHQ-9 at 12 weeks: mindfulness 7.9 (SD 3.8, n = 20); control 9.6 (SD 4.1, n = 21)."
REVIEW_LINE = "Apps reduced depressive symptoms (pooled SMD 0.35, 95% CI 0.20 to 0.50)."
CITED_LINE = "PHQ-9 at 12 weeks: app 8.4 (SD 4.0, n = 75); waitlist 10.1 (SD 4.3, n = 74)."

REVIEW_FILTER = "(systematic review[pt] OR meta-analysis[pt])"
EUROPEPMC_QUERY = 'mindfulness AND depress* AND (METHODS:"PHQ-9" OR RESULTS:"PHQ-9")'
PLAN = SearchPlan(
    levels=[
        # Submitted out of order on purpose: the pipeline runs the levels from the top down.
        SearchLevel(level="primary", query="mindfulness[tiab] AND depress*[tiab]", why="Trials in students."),
        SearchLevel(level="reviews", query=f"mindfulness[tiab] AND depress*[tiab] AND {REVIEW_FILTER}", why="Pooled effects."),
        SearchLevel(level="full_text", query=EUROPEPMC_QUERY, why="PHQ-9 named in the methods or results."),
    ],
    clinicaltrials_queries=["mindfulness AND depression"],
    extra_ids=[],
    summary="Reviews first, then trials, then the registry.",
)


class FakeLLM:
    """Plays the models' parts with fixed answers and records which step ran on which role."""

    def __init__(self):
        self.calls: list[str] = []
        self.screened: list[str] = []  # the record texts screening saw

    def document(self, fulltext, title):
        return fulltext.document_block(title)

    def read_spec(self, paragraph, sink=None):
        self.calls.append("spec")
        return SPEC

    def plan_search(self, spec, toolbox, sink=None):
        self.calls.append("search")
        assert toolbox.count_pubmed(["mindfulness[tiab]", f"mindfulness[tiab] AND {REVIEW_FILTER}"]) == (
            f"#1 4 | mindfulness[tiab]\n#2 1 | mindfulness[tiab] AND {REVIEW_FILTER}"
        )
        assert "Total hits: 4" in toolbox.preview_pubmed("mindfulness", 5)
        assert f"{PREPRINT} | 2024 | PPR | preprint | A smartphone" in toolbox.preview_europepmc(EUROPEPMC_QUERY, 5)
        assert "NCT01234567" in toolbox.preview_trials("mindfulness", 5)
        return PLAN

    def screen(self, spec, record_text, sink=None):
        self.calls.append("screen")
        self.screened.append(record_text)
        excluded = "mouse" in record_text.lower()
        review = "systematic review" in record_text.lower()
        return ScreenDecision(
            decision="exclude" if excluded else "include",
            reason="Animal study." if excluded else "Relevant review." if review else "Relevant trial.",
            study_type="other" if excluded else "systematic_review_or_meta_analysis" if review else "rct",
            reports_relevant_outcome="no" if excluded else "yes",
        )

    def extract(self, spec, document, sink=None):
        self.calls.append("extract")
        return self._first_pass(document["title"])

    @staticmethod
    def _first_pass(title):
        if title.startswith("A mindfulness app"):
            return _extraction([_result([
                _value("App", "n", 100, HEADER), _value("Waitlist", "n", 100, HEADER),
                _value("App", "mean", 8.1, ROW), _value("App", "sd", 4.2, ROW),
                _value("Waitlist", "mean", 10.3, ROW), _value("Waitlist", "sd", 4.5, ROW),
            ])])
        if title.startswith("Brief mindfulness"):
            return _extraction([_result([
                _value("Mindfulness", "n", 60, PDF_LINE, "Page 1"),
                _value("Waitlist", "n", 60, PDF_LINE, "Page 1"),
                _value("Mindfulness", "mean", 9.0, PDF_LINE, "Page 1"),
                _value("Waitlist", "mean", 11.0, PDF_LINE, "Page 1"),
            ])])
        if title.startswith("Mindfulness apps for depressive symptoms"):
            return _extraction([OutcomeResult(
                outcome_measure="Depressive symptoms", timepoint="post-treatment", analysis_population="unclear",
                adjusted="unadjusted", relevance="partial", confidence="high", concerns=[],
                values=[ReportedValue(group="App vs control", statistic=s, value=v, describes="Pooled SMD of depressive symptoms",
                                      quote=REVIEW_LINE, location="Abstract")
                        for s, v in (("standardized_mean_difference", 0.35), ("ci_lower", 0.2), ("ci_upper", 0.5))],
            )])
        if title.startswith("Smartphone mindfulness"):
            return _extraction([_result([
                _value("App", "mean", 8.4, CITED_LINE, "Abstract"), _value("App", "sd", 4.0, CITED_LINE, "Abstract"),
                _value("App", "n", 75, CITED_LINE, "Abstract"), _value("Waitlist", "mean", 10.1, CITED_LINE, "Abstract"),
                _value("Waitlist", "sd", 4.3, CITED_LINE, "Abstract"), _value("Waitlist", "n", 74, CITED_LINE, "Abstract"),
            ])])
        if title.startswith("Mindfulness app use"):
            return _extraction([_result([
                _value("App", "mean", 9.2, PMC_ONLY_LINE, "Abstract"), _value("App", "sd", 4.1, PMC_ONLY_LINE, "Abstract"),
                _value("App", "n", 40, PMC_ONLY_LINE, "Abstract"), _value("Control", "mean", 10.8, PMC_ONLY_LINE, "Abstract"),
            ])])
        if title.startswith("A smartphone mindfulness programme"):
            return _extraction([_result([
                _value("App", "mean", 7.5, PREPRINT_LINE, "Abstract"), _value("App", "sd", 3.9, PREPRINT_LINE, "Abstract"),
                _value("Waitlist", "mean", 9.1, PREPRINT_LINE, "Abstract"), _value("Waitlist", "sd", 4.2, PREPRINT_LINE, "Abstract"),
                _value("App", "n", 30, PREPRINT_LINE, "Abstract"), _value("Waitlist", "n", 30, PREPRINT_LINE, "Abstract"),
            ])])
        if title.startswith("A Randomized Trial"):
            return _extraction([_result([
                _value("Mindfulness App", "mean", 8.1, "Values: Mindfulness App: 8.1 (SD 4.2) | Waitlist: 10.3 (SD 4.5)", "Results"),
                _value("Mindfulness App", "sd", 4.2, "Values: Mindfulness App: 8.1 (SD 4.2) | Waitlist: 10.3 (SD 4.5)", "Results"),
                _value("Mindfulness App", "n", 98, "Analyzed (participants): Mindfulness App: 98 | Waitlist: 97", "Results"),
            ])])
        # The abstract-only pilot: the worker gets one number wrong, so the manager must step in.
        return _extraction([_result([
            _value("Mindfulness", "mean", 7.7, ABSTRACT_LINE, "Abstract"),
            _value("Mindfulness", "n", 20, ABSTRACT_LINE, "Abstract"),
        ], concerns=["pilot study"])])

    def study_context(self, spec, document, extraction, sink=None):
        """Reads every result as an ITT ANCOVA, quoting the methods where the fixture has them: the PMC
        paper says its analyses followed the intention-to-treat principle; the others quote nothing."""
        self.calls.append("context")
        pmc = document["title"].startswith("A mindfulness app")
        review = document["title"].startswith("Mindfulness apps for depressive symptoms")
        return StudyContext(
            design="meta-analysis of randomized trials" if review else "two-arm parallel RCT, individually randomized",
            sample_size="", sample_size_location="",
            results=[ResultContext(
                result=i + 1, population="all randomized (ITT)", treatments="mindfulness app vs waitlist",
                variable=f"{r.outcome_measure} at {r.timepoint}", intercurrent_events="not stated",
                summary_measure="pooled standardized mean difference" if review else "difference in mean PHQ-9",
                model="random-effects meta-analysis" if review else "ANCOVA adjusted for baseline",
                missing_data="not stated",
                quote="Analyses followed the intention-to-treat principle." if pmc else "",
                location="Methods" if pmc else "",
                numbers_are="a standardized effect, no SD" if review else "observed means and SDs at 12 weeks",
                vs_plan="different" if review else "same",
                differences=["standardized scale, not PHQ-9 points"] if review else [],
                use="Gives no SD on the PHQ-9 scale." if review else "The SDs can serve as the outcome SD.",
            ) for i, r in enumerate(extraction.results)],
            notes="",
        )

    def check_overlap(self, spec, records, sink=None):
        """Finds what the fixtures hold: a trial and its registry record share a registration number,
        and the meta-analysis cites trials that are also in the set."""
        self.calls.append("overlap")
        self.overlap_records = records
        groups = []
        by_nct: dict[str, list[dict]] = {}
        for r in records:
            for nct in r["nct_ids"]:
                by_nct.setdefault(nct, []).append(r)
        for nct, same in by_nct.items():
            if len(same) > 1:
                paper = next((r for r in same if r["source"] == "pubmed"), same[0])
                groups.append(OverlapGroup(records=[r["key"] for r in same], relation="same_study",
                                           evidence=f"Both registered as {nct}.", use=paper["key"]))
        for review in (r for r in records if "Meta-Analysis" in r["publication_types"]):
            cited = [r["key"] for r in records if review["key"] in r["cited_by"]]
            if cited:
                groups.append(OverlapGroup(records=[review["key"], *cited], relation="review_includes_study",
                                           evidence="The review cites these trials.", use=""))
        return OverlapReport(groups=groups, notes="")

    def summarize(self, spec, overlap, evidence, sink=None):
        """Lists, for each effect size to find, the values of the statistics it names, and names the
        smallest and largest SD by key, as the real summary would."""
        self.calls.append("summarize")
        self.evidence = evidence
        direct_sds = sorted((v for v in evidence["values"] if v["statistic"] == "sd" and v["relevance"] == "direct"),
                            key=lambda v: v["value"])
        estimands_by_key = {e["key"]: e for e in evidence["estimands"]}
        needs = []
        for need in spec.statistics_needed:
            keys = [v["key"] for v in evidence["values"] if v["statistic"] in need.statistics and v["relevance"] == "direct"]
            sds = sorted((v for v in evidence["values"] if v["key"] in keys and v["statistic"] == "sd"), key=lambda v: v["value"])
            comparison = f"The SDs run from [{sds[0]['key']}] to [{sds[-1]['key']}]." if sds else "No SD is reported."
            same = [v["key"] for v in evidence["values"]
                    if v["key"] in keys and v["estimand"] and estimands_by_key[v["estimand"]]["vs_plan"] == "same"]
            estimands = (f"[{same[0]}] estimates the planned difference in mean PHQ-9 with an ANCOVA."
                         if same else "No value estimates the planned quantity.")
            needs.append(NeedSummary(need=need.purpose, values=keys, estimands=estimands, comparison=comparison))
        effects = [v for v in evidence["values"] if v["statistic"] == "standardized_mean_difference" and v["quote_found"]]
        recommendations = []
        for outcome in spec.outcomes:
            if not effects:
                continue
            selected = effects[0]
            recommendations.append(PlanningRecommendation(
                outcome=outcome.name, status="provisional", estimand="App versus control for depressive symptoms; confirm timepoint and analysis.",
                choice={"method": "reported", "inputs": [selected["key"]], "target_value": .3,
                        "rationale": "A smaller planning assumption than the review estimate; clinical importance remains to be established."},
                justification=f"{selected['record']} supplies the quantitative basis; the target is a provisional planning judgment.",
                clinical_importance="unknown", importance_basis="No supported clinical-importance threshold was found.",
                realism="The review provides indirect evidence for planning.", evidence_quality="Applicability and underlying trial bias need assessment.",
                assumptions=["The review's direction and contrast are applicable."], sensitivity=[],
                nuisance_parameters=[], gaps=["Clinical importance and target-population applicability."]))
        return EvidenceSummary(
            recommendations=recommendations,
            overview=f"{len(evidence['records'])} records report PHQ-9 in students; the largest SD is [{direct_sds[-1]['key']}], "
            f"in {direct_sds[-1]['record']}."
            if direct_sds else f"{len(evidence['records'])} records report PHQ-9 in students.",
            needs=needs,
            overlap="The registry record reports the same participants as its paper.",
            caveats=["Check whether the SDs are of change scores."],
            gaps=["No dropout rates."],
        )

    def adjudicate(self, spec, document, worker, flags, sink=None):
        self.calls.append("adjudicate")
        assert flags
        # Only the pilot's first pass is wrong; a paper reviewed for any other reason (the random
        # audit) comes back as it was.
        if not document["title"].startswith("Mindfulness groups"):
            return self._first_pass(document["title"])
        return _extraction([_result([
            _value("Mindfulness", "mean", 7.9, ABSTRACT_LINE, "Abstract"),
            _value("Mindfulness", "sd", 3.8, ABSTRACT_LINE, "Abstract"),
            _value("Mindfulness", "n", 20, ABSTRACT_LINE, "Abstract"),
            _value("Control", "mean", 9.6, ABSTRACT_LINE, "Abstract"),
            _value("Control", "sd", 4.1, ABSTRACT_LINE, "Abstract"),
            _value("Control", "n", 21, ABSTRACT_LINE, "Abstract"),
        ], concerns=[])])


def sse(events: list[dict]) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def streamed(model, payload, stop_reason="end_turn"):
    text = json.dumps(payload)
    return sse([
        {"type": "message_start", "message": {"id": "msg_1", "type": "message", "role": "assistant", "model": model,
                                              "content": [], "stop_reason": None, "stop_sequence": None,
                                              "usage": {"input_tokens": 100, "output_tokens": 1}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text[:20]}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text[20:]}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None}, "usage": {"output_tokens": 50}},
        {"type": "message_stop"},
    ])


# -- OpenAI's Responses API ----------------------------------------------------------------------

OPENAI_USAGE = {"input_tokens": 120, "input_tokens_details": {"cached_tokens": 20}, "output_tokens": 30,
                "output_tokens_details": {"reasoning_tokens": 10}}


def openai_sse(items: list[dict], model: str, usage: dict | None = None, status: str = "completed", incomplete: dict | None = None) -> bytes:
    """A Responses stream: each output item announced and finished, then the response completed (or
    incomplete). The completed response leaves `output` empty, as plan usage's stream may."""
    response = {"id": "resp_1", "object": "response", "model": model, "status": status, "output": [],
                "usage": usage or OPENAI_USAGE, "incomplete_details": incomplete}
    events = [{"type": "response.created", "response": {**response, "status": "in_progress", "usage": None}}]
    for index, item in enumerate(items):
        events.append({"type": "response.output_item.added", "output_index": index, "item": {**item, "status": "in_progress"}})
        events.append({"type": "response.output_item.done", "output_index": index, "item": item})
    events.append({"type": "response.completed" if status == "completed" else "response.incomplete", "response": response})
    return sse(events)


def message_item(text: str) -> dict:
    return {"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}]}


def reasoning_item() -> dict:
    return {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "opaque-reasoning"}


def function_call_item(name: str, arguments: dict, call_id: str) -> dict:
    return {"type": "function_call", "id": f"fc_{call_id}", "call_id": call_id, "name": name, "namespace": "literature",
            "arguments": json.dumps(arguments), "status": "completed"}


class FakeOpenAI:
    """httpx mock transport handler standing in for the Responses API: each step is answered as
    FakeLLM answers it, so a whole pipeline can run on OpenAILLM."""

    def __init__(self):
        self.llm = FakeLLM()
        self.requests: list[tuple[httpx.Request, dict]] = []
        self.tool_outputs: dict[str, str] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append((request, body))
        return httpx.Response(200, content=self.respond(body), headers={"content-type": "text/event-stream"})

    def respond(self, body: dict) -> bytes:
        """The stream answering one request body (the browser tests serve it to the page)."""
        items = self._search(body) if "tools" in body else [message_item(self._answer(body).model_dump_json())]
        return openai_sse(items, body["model"])

    def _answer(self, body):
        parts = body["input"][0]["content"]
        text = "".join(p["text"] for p in parts if p["type"] == "input_text")
        files = [p for p in parts if p["type"] == "input_file"]
        name = body["text"]["format"]["name"]
        if name == "SearchSpec":
            return self.llm.read_spec(text)
        if name == "ScreenDecision":
            return self.llm.screen(SPEC, text)
        if name == "PaperExtraction":
            first = text.split("\n", 1)[0].removeprefix("Document: ").removesuffix(" (the text layer of its PDF)")
            document = {"title": files[0]["filename"].removesuffix(".pdf") if files else first}
            if "Junior reviewer's extraction" in text:
                return self.llm.adjudicate(SPEC, document, None, ["a flag"])
            return self.llm.extract(SPEC, document)
        if name == "StudyContext":
            first = text.split("\n", 1)[0].removeprefix("Document: ").removesuffix(" (the text layer of its PDF)")
            document = {"title": files[0]["filename"].removesuffix(".pdf") if files else first}
            extraction = PaperExtraction.model_validate(json.loads(text.split(CONTEXT_INPUT, 1)[1]))
            return self.llm.study_context(SPEC, document, extraction)
        if name == "OverlapReport":
            return self.llm.check_overlap(SPEC, json.loads(text.split("Records:\n", 1)[1]))
        if name == "EvidenceSummary":
            data = json.loads(text.split("the values:\n", 1)[1])
            return self.llm.summarize(SPEC, data["overlap"], data)
        raise AssertionError(name)

    def _search(self, body):
        """Explores (with a null max_results, as strict mode sends for "the default"), submits the plan, then stops."""
        self.llm.calls.append("search-turn")
        for item in body["input"]:
            if item.get("type") == "function_call_output":
                self.tool_outputs[item["call_id"]] = item["output"]
        calls = {item["call_id"] for item in body["input"] if item.get("type") == "function_call"}
        if not calls:
            return [reasoning_item(),
                    function_call_item("count_pubmed", {"queries": ["mindfulness[tiab]", f"mindfulness[tiab] AND {REVIEW_FILTER}"]}, "c1"),
                    function_call_item("search_europepmc", {"query": EUROPEPMC_QUERY, "max_results": None}, "c2")]
        if "c3" not in calls:
            return [function_call_item("submit_search_plan", PLAN.model_dump(), "c3")]
        return [message_item("Searched from the reviews down to the full text.")]
