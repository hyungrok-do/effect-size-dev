"""What the summary and the export are given from the manager's reading of each paper's statistical
context: the estimands of the kept results, each record's design and its own sample-size calculation,
and the export's columns."""

from types import SimpleNamespace

from esf.evidence import ESTIMAND_FIELDS, build_evidence, decisions_basis
from esf.schemas import ResultContext, StudyContext
from esf.server import EXPORT_COLUMNS, export_rows
from fixtures import _extraction, _result, _value

LINE = "PHQ-9 at 12 weeks: app 8.1 (SD 4.2, n = 50); waitlist 10.3 (SD 4.5, n = 50)."

# What build_evidence gave each value and each record before the manager read contexts; the reading
# adds one field to each, at the end.
VALUE_FIELDS = [
    "key", "record", "result", "outcome", "timepoint", "group", "statistic", "value", "ci_level", "describes", "n",
    "relevance", "confidence", "decision", "analysis_population", "adjusted", "outcome_type", "independent_groups",
    "concerns", "flags", "quote", "location", "quote_found", "same_participants_as",
]
RECORD_FIELDS = [
    "key", "source", "pmid", "nct_ids", "doi", "title", "authors", "year", "journal", "publication_types", "design",
    "population", "groups", "outcomes", "cited_by", "fulltext_kind", "study_type", "matches_spec", "mismatch_reasons",
    "notes", "appraisal",
]


def _arms(mean, sd):
    return _result([_value("App", "mean", mean, LINE), _value("App", "sd", sd, LINE), _value("App", "n", 50, LINE)])


def _reading(number, vs_plan="same", differences=()):
    return ResultContext(
        result=number, population="all randomized", treatments="app vs waitlist", variable=f"PHQ-9 at 12 weeks (result {number})",
        intercurrent_events="stopping the app: treatment policy", summary_measure="difference in mean PHQ-9",
        model="ANCOVA adjusted for baseline PHQ-9", missing_data="multiple imputation under MAR",
        quote="An ANCOVA adjusted for baseline PHQ-9.", location="Methods",
        numbers_are="observed means and SDs", vs_plan=vs_plan, differences=list(differences),
        use="The SDs can serve as the outcome SD.",
    )


def _done(*readings, design="two-arm parallel RCT", sample_size_check="exact"):
    context = StudyContext(design=design, sample_size="A sample of 100 gives 80% power.", sample_size_location="Methods",
                           results=list(readings), notes="Attrition 10% in each arm.")
    return {"status": "done", "result": context.model_dump(),
            "checks": {"sample_size": sample_size_check, "results": ["exact"] * len(readings)}, "model": "the-manager"}


def _paper(paper_id, results, context):
    return {"id": paper_id, "source": "pubmed", "ext_id": str(paper_id), "title": f"Trial {paper_id}", "status": "extracted",
            "extraction": _extraction(results).model_dump(), "verification": None, "context": context}


def papers():
    return [
        # Both results read; the investigator excluded the second.
        _paper(1, [_arms(8.1, 4.2), _arms(9.0, 4.4)], _done(_reading(1), _reading(2))),
        # Only the second result read: the first has no estimand. Its sample-size quote wasn't found in the paper.
        _paper(2, [_arms(8.4, 4.0), _arms(7.9, 3.9)],
               _done(_reading(2, "close", ["completers rather than all randomized", "8 weeks"]), design="cluster RCT",
                     sample_size_check="missing")),
        # Being read, or the reading failed: no estimands, no context on the record.
        _paper(3, [_arms(9.2, 4.1)], {"status": "running"}),
        _paper(4, [_arms(9.5, 4.3)], {"status": "error", "error": "overloaded"}),
        # Read, but excluded as a whole.
        _paper(5, [_arms(7.5, 3.9)], _done(_reading(1))),
        # Extracted before the manager read contexts.
        _paper(6, [_arms(7.7, 3.8)], None),
    ]


DECISIONS = {1: {1: {"decision": "exclude"}}, "5": {"-1": {"decision": "exclude"}}}


def test_the_kept_results_carry_their_estimands_and_the_values_name_them():
    evidence = build_evidence(papers(), DECISIONS, None)
    assert list(evidence) == ["records", "estimands", "values"]
    assert [r["key"] for r in evidence["records"]] == ["R1", "R2", "R3", "R4", "R6"]
    # Each estimand names its result as the values do (record:index from 0).
    assert [(e["key"], e["record"], e["result"]) for e in evidence["estimands"]] == [("E1", "R1", "R1:0"), ("E2", "R2", "R2:1")]
    first = evidence["estimands"][0]
    assert list(first) == ["key", "record", "result", "outcome", "timepoint", *ESTIMAND_FIELDS]
    assert (first["outcome"], first["timepoint"], first["model"], first["variable"]) == (
        "PHQ-9", "12 weeks", "ANCOVA adjusted for baseline PHQ-9", "PHQ-9 at 12 weeks (result 1)")
    second = evidence["estimands"][1]
    assert second["vs_plan"] == "close" and second["differences"] == ["completers rather than all randomized", "8 weeks"]
    by_result = {v["result"]: v["estimand"] for v in evidence["values"]}
    assert all(by_result[e["result"]] == e["key"] for e in evidence["estimands"])

    by_record = {}
    for v in evidence["values"]:
        by_record.setdefault(v["record"], []).append(v["estimand"])
    # The excluded result's values are left out with it; a result the manager didn't read has none.
    assert by_record == {"R1": ["E1"] * 3, "R2": [None] * 3 + ["E2"] * 3, "R3": [None] * 3, "R4": [None] * 3, "R6": [None] * 3}
    assert [v["value"] for v in evidence["values"] if v["record"] == "R1"] == [8.1, 4.2, 50]
    # The values and records keep every field they had, with the reading added last.
    assert all(list(v) == [*VALUE_FIELDS, "estimand"] for v in evidence["values"])
    assert all(list(r) == [*RECORD_FIELDS, "context"] for r in evidence["records"])
    r2 = next(v for v in evidence["values"] if v["result"] == "R2:1" and v["statistic"] == "sd")
    assert (r2["key"], r2["value"], r2["n"], r2["outcome_type"], r2["independent_groups"]) == ("V8", 3.9, 50, "unclear", None)

    # Each read record carries the design and the paper's own sample-size calculation, as the manager stated them.
    contexts = {r["key"]: r["context"] for r in evidence["records"]}
    assert contexts == {
        "R1": {"design": "two-arm parallel RCT", "sample_size": "A sample of 100 gives 80% power.", "sample_size_found": True,
               "notes": "Attrition 10% in each arm."},
        "R2": {"design": "cluster RCT", "sample_size": "A sample of 100 gives 80% power.", "sample_size_found": False,
               "notes": "Attrition 10% in each arm."},
        "R3": None, "R4": None, "R6": None,
    }
    # The design the worker extracted stays on the record beside the manager's.
    assert {r["design"] for r in evidence["records"]} == {"parallel RCT"}


def test_a_summary_stays_current_until_a_papers_context_is_read():
    before = papers()
    basis = decisions_basis(before, DECISIONS)
    # The context is part of the basis only once it is done.
    assert [len(row) for row in basis] == [9, 9, 8, 8, 9, 8]
    assert basis[0][8] == before[0]["context"]["result"]
    after = papers()
    after[2]["context"] = _done(_reading(1))
    assert decisions_basis(after, DECISIONS) != basis
    failed = papers()
    failed[2]["context"] = {"status": "error", "error": "overloaded"}
    assert decisions_basis(failed, DECISIONS) == basis
    changed = papers()
    changed[0]["context"]["result"]["results"][0]["vs_plan"] = "different"
    assert decisions_basis(changed, DECISIONS) != basis
    # Without any reading, the basis is what it was before the manager read contexts.
    unread = papers()
    for paper in unread:
        paper["context"] = None
    assert [row[:8] for row in basis] == decisions_basis(unread, DECISIONS)


def test_the_export_carries_the_managers_reading_of_each_result():
    rows = export_rows(SimpleNamespace(decisions=lambda _: {}, papers=lambda *args, **kwargs: papers()), "project", False)
    assert all(list(row) == EXPORT_COLUMNS for row in rows)
    at = EXPORT_COLUMNS.index
    assert at("confidence") + 1 == at("design") and at("differences") + 1 == at("group")
    assert EXPORT_COLUMNS[at("design"):at("group")] == [
        "design", "estimand_population", "estimand_treatments", "estimand_variable", "intercurrent_events", "summary_measure",
        "model", "missing_data", "numbers_are", "vs_plan", "differences",
    ]
    read = next(r for r in rows if r["paper_id"] == 1 and r["result"] == 1)
    assert {column: read[column] for column in EXPORT_COLUMNS[at("design"):at("group")]} == {
        "design": "two-arm parallel RCT", "estimand_population": "all randomized", "estimand_treatments": "app vs waitlist",
        "estimand_variable": "PHQ-9 at 12 weeks (result 1)", "intercurrent_events": "stopping the app: treatment policy",
        "summary_measure": "difference in mean PHQ-9", "model": "ANCOVA adjusted for baseline PHQ-9",
        "missing_data": "multiple imputation under MAR", "numbers_are": "observed means and SDs", "vs_plan": "same", "differences": "",
    }
    close = next(r for r in rows if r["paper_id"] == 2 and r["result"] == 2)
    assert close["differences"] == "completers rather than all randomized; 8 weeks" and close["vs_plan"] == "close"
    assert close["design"] == "cluster RCT"
    # Empty where the manager has no reading of the result: not read, being read, failed, or never asked.
    for row in rows:
        if (row["paper_id"], row["result"]) in {(2, 1), (3, 1), (4, 1), (6, 1)}:
            assert all(row[column] == "" for column in EXPORT_COLUMNS[at("design"):at("group")]), row
