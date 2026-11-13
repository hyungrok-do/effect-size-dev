"""Scientific boundaries, source linkage and bounded orchestration for table reports.

Synthetic fixtures test software behavior, not real-paper extraction accuracy.
"""
import copy
import json
import subprocess
from pathlib import Path

import pytest
from esf.reporting import analysis_rows, facts, finalize_tables, profile_checks
from esf.schemas import AnalysisProfile, ContextAudit, FormulationDetails, PopulationDetails, TargetParameter

QUOTE = 'Adults were randomized to treatment or control; the risk difference at 12 weeks was -0.10 (95% CI -0.18 to -0.02).'


def profile(population='Adults'):
    values = {
        'intended': population, 'eligibility': 'Age 18 or older', 'recruited': population, 'analysis_set': 'All randomized',
        'subgroup': 'Entire eligible population', 'setting': 'Outpatient clinics', 'sample_sizes': '200',
        'covariate_distribution': 'Age distribution reported', 'design': 'Parallel randomized trial',
        'equation': 'E(Y|A) = alpha + beta A', 'treatment': 'Treatment', 'comparator': 'Control',
        'outcome': 'Event', 'time_horizon': '12 weeks', 'adjustment': 'None', 'dependence': 'Independent participants',
        'estimation': 'Difference in arm risks', 'missingness_censoring': 'Complete follow-up',
        'intercurrent_events': 'Treatment policy', 'scientific_estimand': 'Assignment risk difference',
        'reported_parameter': 'Risk difference', 'coefficient_mapping': 'beta', 'conditioning': 'marginal',
        'interpretation': 'Randomized assignment effect', 'scale': 'Risk difference', 'units': 'Probability',
        'direction': 'Treatment minus control',
    }
    return {section: {field: {'value': values[field], 'status': 'reported', 'quote': QUOTE, 'location': 'Table 2', 'rationale': ''}
                      for field in model.model_fields}
            for section, model in [('population', PopulationDetails), ('formulation', FormulationDetails), ('target', TargetParameter)]}


def example():
    analyses, values = [], []
    for i in [1, 2]:
        p = profile()
        checked = profile_checks({'results': [{'result': 1, 'profile': p}]}, QUOTE, 1,
                                 {'results': [{'result': 1, 'verdict': 'supported', 'issues': []}]})[0]
        analyses.append({'id': f'R{i}:0', 'record': f'R{i}', 'title': f'Synthetic trial {i}', 'outcome': 'Event', 'timepoint': '12 weeks',
                         'population': 'Adults', 'analysis_set': 'itt', 'adjusted': 'unadjusted', 'profile': p,
                         'verification': checked['status'], 'facts': checked['facts'], 'flags': checked['flags'],
                         'source_type': 'pmc_xml', 'decision': None})
        for metric, val in [('risk_difference', -.1), ('ci_lower', -.18), ('ci_upper', -.02)]:
            values.append({'key': f'V{len(values)+1}', 'result': f'R{i}:0', 'record': f'R{i}', 'statistic': metric,
                           'value': val, 'ci_level': 95 if metric.startswith('ci_') else None, 'quote_found': True,
                           'flags': [], 'group': 'Treatment vs control', 'describes': metric, 'quote': QUOTE, 'location': 'Table 2'})
    group = dict(analyses=['R1:0', 'R2:0'], population='Adults', comparison='Treatment vs control', outcome='Event',
                 timepoint='12 weeks', parameter='Risk difference', status='direct_synthesis', reason='Same native marginal risk contrast',
                 differences=[], mapping='identity', required_inputs=['Native effects and uncertainty'], assumptions=['Study independence'],
                 methodological_sources=[], inputs_available='yes', dependence='Distinct randomized trials')
    return {'groups': [group]}, {'analyses': analyses, 'values': values, 'records': [], 'estimands': []}


def test_compatible_results_remain_exact_and_are_never_pooled():
    plan, evidence = example()
    plan['groups'][0]['combined_effect'] = 999
    original = copy.deepcopy(evidence)
    report = finalize_tables(plan, evidence)
    assert report['groups'][0]['status'] == 'direct_synthesis'
    assert report['groups'][0]['combined_effect'] is None
    assert report['groups'][0]['combined_uncertainty'] is None
    assert len(report['findings']) == 1
    assert report['findings'][0]['values'] == [f'V{i}' for i in range(1, 7)]
    assert evidence == original


def test_different_populations_are_separate_even_when_effects_identical():
    plan, evidence = example()
    evidence['analyses'][1]['profile']['population']['intended']['value'] = 'Older adults'
    report = finalize_tables(plan, evidence)
    assert [r['population'] for r in report['findings']] == ['Adults', 'Older adults']
    assert report['groups'][0]['status'] == 'insufficient_information'


def test_planner_cannot_reverse_a_contrast_without_a_mapping():
    plan, evidence = example()
    plan['groups'][0]['comparison'] = 'Control vs treatment'
    report = finalize_tables(plan, evidence)
    assert report['groups'][0]['status'] == 'insufficient_information'
    assert report['findings'][0]['comparison'] == 'Treatment vs Control'


@pytest.mark.parametrize('kind', ['same_study', 'possible_overlap', 'review_includes_study', 'reviews_share_studies'])
def test_overlapping_reports_cannot_be_counted_as_independent(kind):
    plan, evidence = example()
    report = finalize_tables(plan, evidence, {'groups': [{'records': ['R1', 'R2'], 'relation': kind}]})
    assert report['groups'][0]['status'] == 'insufficient_information'
    assert any(kind in n for n in report['groups'][0]['validation_notes'])


def test_missing_or_fabricated_references_do_not_lose_retained_results():
    plan, evidence = example()
    plan['groups'][0]['analyses'] = ['R1:0', 'R1:0', 'R999:0']
    report = finalize_tables(plan, evidence)
    ids = [i for g in report['groups'] for i in g['analyses']]
    assert ids == ['R1:0', 'R2:0']
    assert all(g['status'] == 'insufficient_information' for g in report['groups'])


@pytest.mark.parametrize('field', ['ci_level', 'group'])
def test_uncertainty_cannot_be_borrowed_or_assumed(field):
    plan, evidence = example()
    evidence['values'][1][field] = None if field == 'ci_level' else 'Unrelated group'
    assert finalize_tables(plan, evidence)['groups'][0]['status'] == 'insufficient_information'


def test_quote_presence_is_not_a_semantic_audit():
    p = profile()
    p['target']['coefficient_mapping']['status'] = 'reconstructed'
    context = {'results': [{'result': 1, 'profile': p}]}
    checks = profile_checks(context, QUOTE, 1, {'results': [{'result': 1, 'verdict': 'needs_revision', 'issues': ['Wrong reference arm']}]})
    assert checks[0]['status'] == 'needs_attention'
    assert 'Wrong reference arm' in checks[0]['flags']
    assert any('missing derivation' in f for f in checks[0]['flags'])


def test_omitted_audit_results_and_legacy_readings_remain_unverified():
    checks = profile_checks({'results': [{'result': 1, 'profile': profile()}]}, QUOTE, 2, {'results': []})
    assert all(c['status'] == 'needs_attention' for c in checks)
    assert checks[1]['facts'] == []
    plan, evidence = example()
    for a in evidence['analyses']:
        a['profile'], a['facts'], a['verification'] = None, [], 'needs_attention'
    assert len(finalize_tables(plan, evidence)['findings']) == 2


def test_incomplete_transformation_is_not_ready_to_combine():
    plan, evidence = example()
    plan['groups'][0].update(status='transform_then_synthesize', mapping='OR to RD')
    assert finalize_tables(plan, evidence)['groups'][0]['status'] == 'insufficient_information'


def test_python_browser_parity_for_tables_and_source_checks():
    plan, evidence = example()
    payload = {'plan': plan, 'evidence': evidence, 'context': {'results': [{'result': 1, 'profile': profile()}]},
               'text': QUOTE, 'audit': {'results': [{'result': 1, 'verdict': 'supported', 'issues': []}]}}
    root = Path(__file__).resolve().parents[1]
    script = """import fs from 'node:fs'; import { finalizeTables, profileChecks } from './site/lib/reporting.js';
const d=JSON.parse(fs.readFileSync(0,'utf8')); console.log(JSON.stringify({tables:finalizeTables(d.plan,d.evidence),checks:profileChecks(d.context,d.text,1,d.audit)}));"""
    result = subprocess.run(['node', '--input-type=module', '-e', script], input=json.dumps(payload), text=True,
                            capture_output=True, cwd=root, check=True)
    got = json.loads(result.stdout)
    assert got['tables'] == finalize_tables(plan, evidence)
    assert got['checks'] == profile_checks(payload['context'], QUOTE, 1, payload['audit'])


def test_semantic_audit_repairs_once_then_preserves_unresolved_flags(tmp_path):
    from test_pipeline import make_pipeline
    from fixtures import SPEC, _extraction, _result, _value
    pipeline, store, llm = make_pipeline(tmp_path)
    project_id = store.create_project('A study of treatment versus control in adults.')
    # Reuse the normal store/source path rather than mock storage internals.
    store.add_paper(project_id, source='pubmed', ext_id='1', title='Synthetic trial', status='extracted')
    paper_id = store.papers(project_id)[0]['id']
    extraction = _extraction([_result([_value('Treatment vs control', 'risk_difference', -.1, QUOTE, 'Table 2')])])
    base = llm.study_context(SPEC, {'title': 'Synthetic trial'}, extraction)
    base.results[0].profile = AnalysisProfile.model_validate(profile())
    calls = []
    def interpret(*args, **kwargs):
        calls.append(('interpret', kwargs.get('feedback')))
        return base.model_copy(deep=True)
    def audit(*args, **kwargs):
        calls.append(('audit', None))
        return ContextAudit(results=[{'result': 1, 'verdict': 'needs_revision', 'issues': ['Wrong coefficient mapping']}])
    llm.study_context, llm.audit_context = interpret, audit
    pipeline._read_context(SPEC, paper_id, {'title': 'Synthetic trial'}, extraction, QUOTE, None)
    result = store.paper(paper_id)['context']
    assert [c[0] for c in calls] == ['interpret', 'audit', 'interpret', 'audit']
    assert result['repair_attempted'] and len(result['audit_history']) == 2
    assert result['profile_checks'][0]['status'] == 'needs_attention'
    assert result['status'] == 'done'  # partial supported information is retained


def test_tables_render_with_source_links_and_safe_readable_csv():
    plan, evidence = example()
    # Source-derived strings are untrusted text in both DOM and spreadsheet exports.
    for a in evidence['analyses']:
        a['profile']['population']['intended']['value'] = '=HYPERLINK("https://example.org", "<img onerror=bad()>")'
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(['node', 'tests/reporting-dom.mjs'], input=json.dumps({'plan': plan, 'evidence': evidence}),
                            text=True, capture_output=True, cwd=root, check=True)
    assert json.loads(result.stdout) == {'tables': 6, 'findings': 1, 'values': 6, 'csv': 'population-findings.csv'}
