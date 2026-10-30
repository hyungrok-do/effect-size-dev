"""Numerical, provenance and cross-edition checks for head-agent planning recommendations."""
import copy
import json
import subprocess
from pathlib import Path

import pytest

from esf.planning import finalize_summary, resolve_choice
from esf.schemas import EvidenceSummary, PlanningRecommendation, SearchSpec
from esf.verify import verify
from fixtures import SPEC, _extraction, _result, _value

ROOT = Path(__file__).resolve().parents[1]


def example(binary=False):
    data = [('A', 'mean', 8), ('B', 'mean', 10), ('A', 'sd', 4), ('B', 'sd', 4), ('A', 'n', 50), ('B', 'n', 50)]
    if binary:
        data = [('A', 'events', 20), ('A', 'n', 100), ('B', 'events', 30), ('B', 'n', 100)]
    values = [dict(key=f'V{i+1}', record='R1', result='R1:0', outcome='PHQ-9' if not binary else 'Response',
                   timepoint='12 weeks', group=g, statistic=st, value=n, describes='Endpoint', quote_found=True,
                   flags=[], decision='include', independent_groups=True, adjusted='unadjusted',
                   outcome_type='binary' if binary else 'continuous', relevance='direct', confidence='high', concerns=[],
                   estimand='E1')
              for i, (g, st, n) in enumerate(data)]
    # The manager's reading of the result: the same estimand as planned, its model's quote found in the paper.
    estimands = [dict(key='E1', record='R1', result='R1:0', vs_plan='same', differences=[], model_quote='exact')]
    records = [dict(key='R1', design='parallel RCT', fulltext_kind='pmc_xml', study_type='rct', matches_spec=True,
                    appraisal=[dict(domain=d, quote_found=True) for d in
                               ['randomization', 'deviations', 'missing_outcomes', 'measurement', 'selective_reporting']])]
    return {'records': records, 'estimands': estimands, 'values': values}


def choice(method='mean_difference', target=None):
    count = {'mean_difference': 2, 'cohen_d': 6, 'reported': 1}.get(method, 4)
    return dict(method=method, inputs=[f'V{i+1}' for i in range(count)],
                rationale='Selected compatible trial arms; target follows the stated clinical rationale.', target_value=target)


def spec():
    return {**SPEC.model_dump(), 'outcomes': [dict(name='Depressive symptoms', measure='PHQ-9', role='primary',
                                                 type='continuous', timepoint='12 weeks')],
            'planning': dict(objective='superiority', effect_measure='A minus B', analysis='ANCOVA',
                             analysis_population='itt', intercurrent_events='treatment policy',
                             clinical_importance='The investigator considers this difference important.',
                             clinical_importance_source='Investigator statement')}


def summary(c=None):
    rec = PlanningRecommendation(outcome='Depressive symptoms', status='recommended', estimand='A minus B in PHQ-9 at 12 weeks in students.',
          choice=c or choice(), justification='The directly relevant randomized comparison in R1 supports this choice.',
          clinical_importance='supported', importance_basis='Investigator-supplied importance judgment.',
          realism='R1 provides relevant estimates.', evidence_quality='Source methods were reviewed; remaining uncertainty is acknowledged.',
          assumptions=['Same population and comparator.'], sensitivity=[], nuisance_parameters=['SD [V3].'], gaps=[])
    return EvidenceSummary(overview='Evidence found.', needs=[], overlap='', caveats=[], gaps=[], recommendations=[rec]).model_dump()


def javascript(op, *args):
    script = "import fs from 'node:fs'; import {resolveChoice, finalizeSummary} from './site/lib/planning.js';\n"
    script += "const [op, args] = JSON.parse(fs.readFileSync(0, 'utf8'));\n"
    script += "try { console.log(JSON.stringify((op === 'resolve' ? resolveChoice : finalizeSummary)(...args))); } catch(e) { console.log(JSON.stringify({error:e.message})); }"
    p = subprocess.run(['node', '--input-type=module', '-e', script], input=json.dumps([op, args]), text=True,
                       capture_output=True, check=True, cwd=ROOT)
    return json.loads(p.stdout)


@pytest.mark.parametrize('method, expected', [('mean_difference', -2), ('cohen_d', -.5),
                                             ('risk_difference', -.1), ('risk_ratio', 2/3), ('odds_ratio', 7/12)])
def test_fixed_formulas_and_browser_parity(method, expected):
    evidence = example(binary=method.startswith('risk') or method == 'odds_ratio')
    actual = resolve_choice(choice(method), evidence)
    assert actual['value'] == pytest.approx(expected)
    other = javascript('resolve', choice(method), evidence)
    assert other['value'] == pytest.approx(actual.pop('value'))
    other.pop('value')
    assert other == actual


@pytest.mark.parametrize('field, value, method', [
    ('quote_found', False, 'mean_difference'), ('flags', ['missing quote'], 'mean_difference'),
    ('decision', 'exclude', 'mean_difference'), ('adjusted', 'adjusted', 'mean_difference'),
    ('independent_groups', False, 'cohen_d'), ('outcome_type', 'count', 'risk_difference'),
    ('outcome_type', 'time_to_event', 'risk_ratio'), ('value', True, 'mean_difference'),
    ('value', 1.2, 'risk_difference'),
])
def test_rejects_ineligible_inputs_in_both_editions(field, value, method):
    evidence = example(binary=method.startswith('risk'))
    evidence['values'][0][field] = value
    with pytest.raises(ValueError) as err:
        resolve_choice(choice(method), evidence)
    assert javascript('resolve', choice(method), evidence) == {'error': str(err.value)}


def test_same_result_arm_and_metric_requirements():
    for mutate in [lambda e: e['values'][1].update(result='R2:0'),
                   lambda e: e['values'][2].update(statistic='se'),
                   lambda e: e['values'][3].update(group='A'),
                   lambda e: e['values'][4].update(value=1),
                   lambda e: e['records'][0].update(design='cluster RCT')]:
        evidence = example(); mutate(evidence)
        with pytest.raises(ValueError):
            resolve_choice(choice('cohen_d'), evidence)
        assert 'error' in javascript('resolve', choice('cohen_d'), evidence)
    evidence = example()
    evidence['values'][0]['statistic'] = evidence['values'][1]['statistic'] = 'mean_change'
    with pytest.raises(ValueError):
        resolve_choice(choice('cohen_d'), evidence)
    for i in (2, 3):
        evidence['values'][i]['statistic'] = 'sd_change'
    assert resolve_choice(choice('cohen_d'), evidence)['value'] == -.5


def test_reported_effect_and_no_nuisance_substitution():
    evidence = example()
    with pytest.raises(ValueError, match='nuisance'):
        resolve_choice(choice('reported'), evidence)
    evidence['values'][0].update(statistic='hazard_ratio', value=.75)
    expected = resolve_choice(choice('reported'), evidence)
    assert expected['value'] == .75 and expected['origin'] == 'evidence_estimate'
    assert javascript('resolve', choice('reported'), evidence) == expected
    with pytest.raises(ValueError):
        resolve_choice({**choice(), 'inputs': ['V999', 'V2']}, evidence)


def test_zero_cell_does_not_silently_add_a_correction():
    evidence = example(binary=True)
    evidence['values'][2]['value'] = 0
    for method in ('risk_ratio', 'odds_ratio'):
        with pytest.raises(ValueError, match='no correction'):
            resolve_choice(choice(method), evidence)
        assert 'error' in javascript('resolve', choice(method), evidence)
    assert resolve_choice(choice('risk_difference'), evidence)['value'] == .2


def test_planning_judgment_is_separate_from_observed_effect():
    actual = resolve_choice(choice(target=-1.5), example())
    assert actual['value'] == -1.5 and actual['basis_value'] == -2
    assert actual['origin'] == 'planning_assumption'
    assert javascript('resolve', choice(target=-1.5), example()) == actual
    for target in (0, 2):
        with pytest.raises(ValueError):
            resolve_choice(choice(target=target), example())
    with pytest.raises(ValueError):
        resolve_choice(choice('risk_difference', target=-2), example(True))


@pytest.mark.parametrize('change', ['none', 'importance', 'unreviewed', 'abstract', 'outcome', 'invalid', 'noninferiority', 'omitted',
                                    'unread', 'different_estimand', 'close_estimand', 'close_no_differences',
                                    'model_quote_missing', 'model_quote_approximate', 'no_model_quote'])
def test_final_recommendation_validation_and_browser_parity(change):
    evidence, plan, raw = example(), spec(), summary()
    if change == 'unread': evidence['estimands'] = []
    if change == 'different_estimand': evidence['estimands'][0].update(vs_plan='different', differences=['completers rather than all randomized'])
    if change == 'close_estimand': evidence['estimands'][0].update(vs_plan='close', differences=['completers rather than all randomized; 30% dropout'])
    if change == 'close_no_differences': evidence['estimands'][0].update(vs_plan='close')
    if change == 'model_quote_missing': evidence['estimands'][0].update(model_quote='missing')
    if change == 'model_quote_approximate': evidence['estimands'][0].update(model_quote='approximate')
    if change == 'no_model_quote': evidence['estimands'][0].update(model_quote=None)
    if change == 'importance': plan['planning']['clinical_importance'] = ''
    if change == 'unreviewed': evidence['values'][0]['decision'] = None
    if change == 'abstract': evidence['records'][0]['fulltext_kind'] = 'abstract'
    if change == 'outcome': plan['outcomes'][0]['timepoint'] = '24 weeks'
    if change == 'invalid': evidence['values'][0]['quote_found'] = False
    if change == 'noninferiority': plan['planning']['objective'] = 'noninferiority'
    if change == 'omitted': raw['recommendations'] = []
    actual = finalize_summary(raw, evidence, plan)
    assert javascript('finalize', raw, evidence, plan) == actual
    rec = actual['recommendations'][0]
    expected = ('recommended' if change in ('none', 'model_quote_approximate', 'no_model_quote')
                else 'insufficient_evidence' if change in ('invalid', 'noninferiority', 'omitted') else 'provisional')
    assert rec['status'] == expected
    assert (rec['resolved'] is None) == (expected == 'insufficient_evidence')
    assert actual['overview'] == raw['overview']
    if change == 'unread':
        assert "has not read the selected result's estimand and model" in rec['validation_notes'][-1]
    if change == 'different_estimand':
        assert rec['validation_notes'][-1] == ("The manager read the selected result's estimand as different from the planned one: "
                                               "completers rather than all randomized.")
    # An estimand read as close to the planned one is not the same: the recommendation says how it differs.
    if change == 'close_estimand':
        assert rec['validation_notes'] == ["The manager read the selected result's estimand as close to, not the same as, the planned one: "
                                           "completers rather than all randomized; 30% dropout."]
    if change == 'close_no_differences':
        assert rec['validation_notes'] == ["The manager read the selected result's estimand as close to, not the same as, the planned one."]
    if change == 'model_quote_missing':
        assert rec['validation_notes'] == ["The model the manager described for the selected result is not supported by a quote found in the paper."]


def test_scenarios_match_metric_and_keep_their_assumptions():
    raw = summary()
    raw['recommendations'][0]['sensitivity'] = [dict(label='Smaller effect', choice=choice(target=-1)),
                                               dict(label='Wrong metric', choice=choice('cohen_d'))]
    actual = finalize_summary(raw, example(), spec())
    rec = actual['recommendations'][0]
    assert len(rec['sensitivity']) == 1 and rec['sensitivity'][0]['resolved']['value'] == -1
    assert 'Sensitivity scenario omitted' in rec['validation_notes'][-1]
    assert javascript('finalize', raw, example(), spec()) == actual


def test_appraisal_quotes_are_verified_and_legacy_records_remain_readable():
    original = _extraction([_result([_value('A', 'mean', 8, 'Mean 8.')])])
    data = original.model_dump()
    data['appraisal'] = [dict(domain='randomization', assessment='Allocation was described.', quote='Random allocation was used.', location='Methods')]
    extraction = type(original).model_validate(data)
    good = verify(extraction, 'Mean 8. Random allocation was used.').to_dict()
    bad = verify(extraction, 'Mean 8.').to_dict()
    assert good['appraisal'] == ['exact'] and not any('appraisal' in f for f in good['flags'])
    assert bad['appraisal'] == ['missing'] and 'appraisal' in bad['flags'][0]
    del data['appraisal']
    assert type(original).model_validate(data).appraisal == []
    plan = SPEC.model_dump(); plan.pop('planning')
    assert SearchSpec.model_validate(plan).planning is None


def test_unknown_citations_cannot_support_a_numerical_recommendation():
    for claim in ('Based on R999.', 'The effect is [V999].'):
        raw = summary(); raw['recommendations'][0]['justification'] = claim
        result = finalize_summary(raw, example(), spec())
        assert result['recommendations'][0]['resolved'] is None
        assert javascript('finalize', raw, example(), spec()) == result


def test_appraisal_only_records_and_exclusion_are_respected():
    from esf.evidence import build_evidence, decisions_basis
    paper = dict(id=1, status='extracted', source='pubmed', extraction=dict(study_design='Anchor study',
                 population='Patients', groups=[], results=[], appraisal=[dict(domain='clinical_importance',
                 assessment='Stakeholder threshold.', quote='Important to patients.', location='Results')]),
                 verification=dict(values=[], flags=[], appraisal=['exact']))
    kept = build_evidence([paper], {}, None)
    assert not kept['values'] and kept['records'][0]['appraisal'][0]['quote_found']
    assert not build_evidence([paper], {1: {-1: {'decision': 'exclude'}}}, None)['records']
    old = decisions_basis([paper], {})
    paper['verification']['appraisal'] = ['missing']
    assert decisions_basis([paper], {}) != old
