from esf.verify import (
    Source,
    ValueCheck,
    escalation_reasons,
    in_audit_sample,
    number_in,
    numbers_in,
    plausibility,
    verify,
)
from fixtures import _extraction, _result, _value


def test_numbers_in_reads_journal_typography():
    assert numbers_in("−2.2 (−3.4 to −1.0)") == [-2.2, -3.4, -1.0]
    assert numbers_in("mean 8·1 (SD 4·2)") == [8.1, 4.2]  # Lancet decimal point
    assert numbers_in("n = 1,234 of 2,000") == [1234, 2000]
    assert numbers_in("p<.001; 10-20 weeks") == [0.001, 10, 20]
    assert numbers_in("PHQ-9 at 12 weeks: 8.1%") == [-9, 12, 8.1]


def test_number_in_accepts_a_sign_written_in_words():
    assert number_in(-3.1, "reduced by 3.1 points")
    assert number_in(4.2, "8.1 (4.2)")
    assert not number_in(4.25, "8.1 (4.2)")


def test_find_quote_ignores_layout_but_not_content():
    source = Source("Table 2\nPHQ-9, 12 weeks | 8.1 (4.2) |   10.3 (4.5)\nGAD-7 | 6.0 (3.9)")
    assert source.find_quote("PHQ-9, 12 weeks 8.1 (4.2) 10.3 (4.5)") == "exact"
    assert source.find_quote("PHQ-9, 12 weeks | 8.1 (4.2) | 10.4 (4.5)") == "missing"
    long = Source("the mean score in the intervention group was lower at twelve weeks than in the control group")
    assert long.find_quote("the mean score in the intervention group was lower at 12 weeks than in the control group") == "approximate"


def test_value_check_tolerates_scrambled_pdf_text_only():
    check = ValueCheck(quote="missing", number_in_quote=True, number_in_source=True)
    assert not check.ok(quotes_reliable=True)
    assert check.ok(quotes_reliable=False)
    assert not ValueCheck(quote="exact", number_in_quote=False, number_in_source=True).ok(False)


def test_plausibility_flags():
    result = _result([
        _value("A", "n", 100, "q"), _value("A", "sd", 4.0, "q"), _value("A", "se", 4.0, "q"),
        _value("B", "n", 100, "q"), _value("B", "sd", 0.4, "q"),
        _value("A vs B", "ci_lower", 2.0, "q"), _value("A vs B", "ci_upper", 1.0, "q"),
        _value("A vs B", "p_value", 1.5, "q"), _value("A vs B", "hazard_ratio", 0, "q"),
    ])
    flags = plausibility(result)
    assert any("SD and SE for A disagree" in f for f in flags)
    assert any("threefold" in f for f in flags)
    assert any("CI lower bound above upper" in f for f in flags)
    assert any("p-value" in f for f in flags)
    assert any("hazard ratio" in f for f in flags)
    assert plausibility(_result([_value("A", "mean", 8.1, "q")])) == ["means reported without group sizes"]


def test_verify_and_escalation():
    text = "Results: PHQ-9 at 12 weeks was 8.1 (SD 4.2, n = 50) with the app."
    good = _extraction([_result([
        _value("App", "mean", 8.1, "PHQ-9 at 12 weeks was 8.1 (SD 4.2, n = 50)"),
        _value("App", "sd", 4.2, "PHQ-9 at 12 weeks was 8.1 (SD 4.2, n = 50)"),
        _value("App", "n", 50, "PHQ-9 at 12 weeks was 8.1 (SD 4.2, n = 50)"),
    ])])
    report = verify(good, text)
    assert report.flags == []
    assert escalation_reasons(good, report) == []

    bad = _extraction([_result([
        _value("App", "mean", 8.3, "PHQ-9 at 12 weeks was 8.1 (SD 4.2, n = 50)"),
        _value("App", "n", 50, "an invented sentence with 50 in it"),
    ], confidence="low")])
    report = verify(bad, text)
    assert any("8.3 for App is not in its quote" in f for f in report.flags)
    assert any("has a quote not found in the source" in f for f in report.flags)
    reasons = escalation_reasons(bad, report)
    assert any("low confidence" in r for r in reasons)
    assert escalation_reasons(None, None) == ["the worker could not complete the extraction"]


def test_audit_sample_is_deterministic_and_proportional():
    keys = [f"project:{i}" for i in range(2000)]
    picked = [k for k in keys if in_audit_sample(k, 0.1)]
    assert picked == [k for k in keys if in_audit_sample(k, 0.1)]
    assert 120 < len(picked) < 280
    assert not any(in_audit_sample(k, 0.0) for k in keys)
