from copy import deepcopy
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from esf.evidence import build_evidence, decisions_basis
from esf.llm import output_schema
from esf.schemas import PaperExtraction, ReportedValue
from esf.server import export_rows
from fixtures import _extraction, _result, _value


def test_confidence_level_is_required_in_model_schema_but_old_extractions_load():
    schema = output_schema(PaperExtraction)["$defs"]["ReportedValue"]
    assert "ci_level" in schema["required"]
    value = _value("A vs B", "ci_lower", -3.4, "90% CI −3.4 to −1.0").model_dump()
    del value["ci_level"]
    assert ReportedValue.model_validate(value).ci_level is None
    for level in (90, 95, 99, None):
        assert ReportedValue.model_validate({**value, "ci_level": level}).ci_level == level
    for level in (0, 100, -5):
        with pytest.raises(ValidationError):
            ReportedValue.model_validate({**value, "ci_level": level})


def test_confidence_levels_survive_export_and_summary_inputs():
    values = [_value("A vs B", st, n, "90% CI −3.4 to −1.0")
              for st, n in (("ci_lower", -3.4), ("ci_upper", -1.0))]
    for value in values:
        value.ci_level = 90
    paper = {"id": 1, "source": "pubmed", "ext_id": "123", "title": "Trial", "status": "extracted",
             "extraction": _extraction([_result(values)]).model_dump(), "verification": None}
    store = SimpleNamespace(decisions=lambda _: {}, papers=lambda *args, **kwargs: [paper])
    assert [r["ci_level"] for r in export_rows(store, "project", False)] == [90, 90]
    assert [v["ci_level"] for v in build_evidence([paper], {}, None)["values"]] == [90, 90]
    previous = decisions_basis([paper], {})
    changed = deepcopy(paper)
    changed["extraction"]["results"][0]["values"][0]["ci_level"] = 99
    assert decisions_basis([changed], {}) != previous
    for value in paper["extraction"]["results"][0]["values"]:
        del value["ci_level"]
    assert [r["ci_level"] for r in export_rows(store, "project", False)] == ["", ""]
    assert [v["ci_level"] for v in build_evidence([paper], {}, None)["values"]] == [None, None]
