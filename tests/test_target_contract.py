"""Regression cases for the recommendation boundary, independently exercised in both editions."""
import copy
import json
import subprocess

import pytest

from esf.evidence import build_evidence, comparison_basis, comparison_current, decisions_basis
from esf.planning import finalize_summary, resolve_choice
from esf.schemas import EvidenceSummary, PaperExtraction, SearchSpec
from esf.verify import verify_spec_importance
from test_evidence import papers
from test_planning import ROOT, choice, example, javascript, spec, summary
from fixtures import _extraction, _result, _value
from esf.verify import verify


def finish(raw, evidence, plan):
    EvidenceSummary.model_validate(raw)
    SearchSpec.model_validate(plan)
    actual = finalize_summary(raw, evidence, plan)
    assert javascript('finalize', raw, evidence, plan) == actual
    return actual['recommendations'][0]


def reported(metric, unit, value, proposed=None):
    evidence = example()
    evidence['values'][0].update(statistic=metric, unit=unit, value=value,
        group='A vs B', contrast_direction='intervention_over_comparator' if metric.endswith('ratio') else 'intervention_minus_comparator')
    return evidence, choice('reported', proposed)


@pytest.mark.parametrize('unit,basis,target,valid', [
    ('proportion', -.1, -2, False), ('proportion', -2, -.1, False),
    ('proportion', -.1, -.05, True), ('proportion', -1, None, True),
    ('percentage_points', -10, -2, True), ('percentage_points', -10, -101, False),
    ('percentage_points', -101, None, False), ('percentage_points', -100, None, True),
    ('unknown', -.1, None, False), ('ratio', -.1, None, False),
])
def test_reported_risk_difference_uses_explicit_units(unit, basis, target, valid):
    evidence, c = reported('risk_difference', unit, basis, target)
    if valid:
        resolved = resolve_choice(c, evidence)
        assert resolved['value'] == (basis if target is None else target)
        assert resolved['unit'] == unit
        assert javascript('resolve', c, evidence) == resolved
    else:
        with pytest.raises(ValueError) as error:
            resolve_choice(c, evidence)
        assert javascript('resolve', c, evidence) == {'error': str(error.value)}


@pytest.mark.parametrize('kind', ['reversed', 'standardized', 'unit', 'unknown_source', 'unknown_plan', 'legacy'])
def test_final_effect_must_match_the_planned_quantity(kind):
    evidence, plan, raw = example(), spec(), summary()
    if kind == 'reversed': raw['recommendations'][0]['choice']['inputs'] = ['V2', 'V1']
    if kind == 'standardized': raw = summary(choice('cohen_d'))
    if kind == 'unit': plan['outcomes'][0]['target']['unit'] = 'percentage_points'
    if kind == 'unknown_source': evidence['values'][0]['group_role'] = 'unknown'
    if kind == 'unknown_plan': plan['outcomes'][0]['target']['direction'] = 'unknown'
    if kind == 'legacy': plan['outcomes'][0].pop('target')
    rec = finish(raw, evidence, plan)
    assert rec['status'] == ('insufficient_evidence' if kind in ('reversed', 'standardized', 'unit') else 'provisional')
    assert rec['validation_notes']


def test_standardized_effect_requires_the_correct_denominator():
    plan = spec()
    plan['outcomes'][0]['target'].update(metric='standardized_mean_difference', unit='standardized_units', standardizer='change_sd')
    rec = finish(summary(choice('cohen_d')), example(), plan)
    assert rec['status'] == 'insufficient_evidence'
    assert 'standardizing SD' in rec['validation_notes'][0]


@pytest.mark.parametrize('kind', ['passes', 'below', 'individual', 'narrative', 'other_outcome', 'unknown_rule', 'includes_null'])
def test_importance_is_specific_to_the_selected_magnitude(kind):
    plan, raw = spec(), summary(choice(target=-3.0))
    target = plan['outcomes'][0]['target']
    target.update(importance_threshold=-3, importance_quote='Our panel requires at least a 3-point between-group reduction.')
    if kind == 'below': raw = summary(choice(target=-.1))
    if kind == 'individual': target['importance_scope'] = 'individual_change'
    if kind == 'narrative': target['importance_threshold'] = None
    if kind == 'other_outcome':
        plan['outcomes'].append({**copy.deepcopy(plan['outcomes'][0]), 'name':'Other outcome', 'role':'secondary'})
        target['importance_threshold'] = None
    if kind == 'unknown_rule': target['importance_rule'] = 'unknown'
    if kind == 'includes_null': target['importance_rule'] = 'at_least'
    rec = finish(raw, example(), plan)
    assert rec['clinical_importance'] == ('supported' if kind == 'passes' else 'unknown')
    assert rec['status'] == ('recommended' if kind == 'passes' else 'provisional')
    assert rec['resolved']['value'] == (-.1 if kind == 'below' else -3)


@pytest.mark.parametrize('metric,unit,basis,target,boundary,rule,important', [
    ('risk_difference','percentage_points',-10,-2,-3,'at_most',False),
    ('risk_difference','percentage_points',-10,-5,-3,'at_most',True),
    ('risk_difference','proportion',.1,.05,.08,'at_least',False),
    ('hazard_ratio','ratio',.7,.9,.8,'at_most',False),
    ('hazard_ratio','ratio',.7,.75,.8,'at_most',True),
    ('risk_ratio','ratio',1.5,1.1,1.2,'at_least',False),
])
def test_signed_importance_boundaries_on_difference_and_ratio_scales(metric, unit, basis, target, boundary, rule, important):
    evidence, c = reported(metric, unit, basis, target)
    plan = spec()
    plan['outcomes'][0]['target'].update(metric=metric, unit=unit, direction=evidence['values'][0]['contrast_direction'],
        importance_threshold=boundary, importance_rule=rule, importance_quote='The investigator supplied this boundary.')
    rec = finish(summary(c), evidence, plan)
    assert rec['clinical_importance'] == ('supported' if important else 'unknown')


@pytest.mark.parametrize('kind,check,expected', [('model',None,'provisional'), ('unknown',None,'provisional'),
    ('descriptive',None,'recommended'), ('model','missing','provisional'), ('descriptive','missing','provisional'), ('model','exact','recommended')])
def test_absent_model_quote_is_only_exempt_for_descriptive_statistics(kind, check, expected):
    evidence = example()
    evidence['estimands'][0].update(model_kind=kind, model_quote=check)
    assert finish(summary(), evidence, spec())['status'] == expected


@pytest.mark.parametrize('alternative', [0, 2])
def test_sensitivity_cannot_cross_or_equal_the_null(alternative):
    evidence, raw = example(), summary()
    for i, value in enumerate([10+alternative, 10]):
        evidence['values'].append({**evidence['values'][i], 'key':f'V{i+7}', 'value':value, 'result':'R1:1'})
    raw['recommendations'][0]['sensitivity'] = [dict(label='Alternative', choice={**choice(), 'inputs':['V7','V8']})]
    rec = finish(raw, evidence, spec())
    assert rec['sensitivity'] == [] and rec['status'] == 'provisional'
    assert 'non-null direction' in rec['validation_notes'][-1]


def js_module(module, function, *args):
    code = f"import fs from 'node:fs'; import {{ {function} }} from './site/lib/{module}.js'; console.log(JSON.stringify({function}(...JSON.parse(fs.readFileSync(0, 'utf8')))));"
    return json.loads(subprocess.run(['node', '--input-type=module', '-e', code], input=json.dumps(args), cwd=ROOT,
                                    text=True, capture_output=True, check=True).stdout)


def test_plan_and_extraction_versions_control_comparison_and_export():
    paper, plan = papers()[0], spec()
    paper['context'].update(spec_basis=comparison_basis(plan), extraction_basis=copy.deepcopy(paper['extraction']))
    assert js_module('evidence', 'comparisonBasis', plan) == comparison_basis(plan)
    for kind in ['current', 'search_terms', 'importance', 'analysis', 'intercurrent_events', 'timepoint', 'extraction', 'legacy']:
        p, s = copy.deepcopy(paper), copy.deepcopy(plan)
        if kind == 'search_terms': s['search_terms'] = []
        if kind == 'importance': s['outcomes'][0]['target']['importance_threshold'] = -10
        if kind in ('analysis', 'intercurrent_events'): s['planning'][kind] = 'A different plan'
        if kind == 'timepoint': s['outcomes'][0]['timepoint'] = '24 weeks'
        if kind == 'extraction': p['extraction']['results'][0]['values'][0]['value'] += 1
        if kind == 'legacy': p['context'].pop('spec_basis')
        expected = kind in ('current', 'search_terms', 'importance')
        assert comparison_current(p, s) is expected
        assert js_module('evidence', 'comparisonCurrent', p, s) is expected
        evidence = build_evidence([p], {}, None, s)
        assert evidence['estimands'][0]['comparison_current'] is expected
        assert js_module('evidence', 'buildEvidence', [p], {}, None, s) == evidence
    old = decisions_basis([paper], {})
    paper['context']['checks']['results'][0] = 'missing'
    assert decisions_basis([paper], {}) != old


@pytest.mark.parametrize('paragraph,quote,threshold,kept', [
    ('A 3-point reduction is important.', 'A 3-point reduction is important.', -3, True),
    ('A 3-point reduction is important.', 'A 3-point reduction is important.', -4, False),
    ('No boundary specified.', 'A 3-point reduction is important.', -3, False),
])
def test_importance_boundary_must_be_in_the_investigators_paragraph(paragraph, quote, threshold, kept):
    plan = spec()
    plan['outcomes'][0]['target'].update(importance_threshold=threshold, importance_quote=quote)
    raw = SearchSpec.model_validate(plan)
    checked = verify_spec_importance(raw, paragraph).model_dump()
    assert js_module('verify', 'verifySpecImportance', raw.model_dump(), paragraph) == checked
    assert (checked['outcomes'][0]['target']['importance_threshold'] is not None) is kept


def test_old_specs_and_extractions_remain_readable_without_inventing_metadata():
    plan = spec()
    plan['outcomes'][0].pop('target')
    assert SearchSpec.model_validate(plan).outcomes[0].target is None
    old = papers()[0]['extraction']
    restored = PaperExtraction.model_validate(old)
    assert all(v.unit == 'unknown' and v.group_role == 'unknown' for r in restored.results for v in r.values)


@pytest.mark.parametrize('case', json.loads((ROOT / 'tests/reference_effects.json').read_text()), ids=lambda c:c['id'])
def test_published_effects_preserve_metric_units_direction_and_unknown_importance(case):
    # Curated source excerpts test the deterministic path, not live-model extraction accuracy.
    values = []
    for statistic, number in [(case['metric'], case['value']), ('ci_lower', case['ci'][0]), ('ci_upper', case['ci'][1])]:
        v = _value(case['group'], statistic, number, case['quote'])
        v.unit, v.group_role, v.location = case['unit'], 'other', case['location']
        v.contrast_direction = case['direction'] if statistic == case['metric'] else 'unknown'
        v.ci_level = 95 if statistic.startswith('ci_') else None
        v.describes = case['note']
        values.append(v)
    result = _result(values)
    result.outcome_measure, result.timepoint = case['outcome'], case['timepoint']
    result.outcome_type, result.adjusted = case['outcome_type'], case['adjusted']
    extraction = _extraction([result])
    checked = verify(extraction, case['quote'])
    assert not checked.flags
    paper = dict(id=1, status='extracted', source='pubmed', doi=case['doi'], title=case['id'],
                 extraction=extraction.model_dump(), verification=checked.to_dict(), fulltext_kind='abstract')
    plan = spec()
    plan['outcomes'][0].update(name=case['outcome'], measure=case['outcome'], timepoint=case['timepoint'], type=case['outcome_type'])
    plan['outcomes'][0]['target'].update(metric=case['metric'], unit=case['unit'], direction=case['direction'],
                                       importance_threshold=None, importance_quote='')
    evidence = build_evidence([paper], {1:{0:{'decision':'include'}}}, None, plan)
    raw = summary(choice('reported'))
    raw['recommendations'][0].update(outcome=case['outcome'], estimand=case['note'], nuisance_parameters=[])
    rec = finish(raw, evidence, plan)
    assert rec['status'] == 'provisional' and rec['clinical_importance'] == 'unknown'
    assert rec['resolved']['value'] == case['value']
    assert rec['resolved']['unit'] == case['unit']
    assert rec['resolved']['direction'] == case['direction']
    assert [v['ci_level'] for v in evidence['values']] == [None, 95, 95]
    plan['outcomes'][0]['target']['direction'] = case['direction'].replace('intervention_', 'comparator_').replace('_comparator', '_intervention')
    assert finish(raw, evidence, plan)['status'] == 'insufficient_evidence'


def test_unknown_units_and_standardizers_cannot_certify_importance():
    plan, evidence = spec(), example()
    plan['outcomes'][0]['target']['unit'] = 'unknown'
    for v in evidence['values']: v['unit'] = 'unknown'
    rec = finish(summary(), evidence, plan)
    assert rec['status'] == 'provisional' and rec['clinical_importance'] == 'unknown'
    plan['outcomes'][0]['target'].update(metric='standardized_mean_difference', unit='standardized_units')
    rec = finish(summary(choice('cohen_d')), evidence, plan)
    assert rec['status'] == 'provisional' and rec['clinical_importance'] == 'unknown'
