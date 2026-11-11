"""Output contract (ARTICLE / ASSEMBLY databases) and the new Stage 1 relation fields."""
from copy import deepcopy

import pytest

from facade_dspy import rules
from facade_dspy.schema import PageResult, validate, validate_output

BOX = [.1, .1, .2, .2]
SRC = [dict(page=42, kind='detail', bbox=BOX, text='A1')]


def article(pid, role, kind='bar', **kw):
    return {**dict(profile_id=pid, profile_role=role, article_type=kind, sources=SRC, evidence_status='explicit'), **kw}


def detail(**kw):
    value = dict(catalogue='demo.pdf', page=42, detail_id='d1', view_type='profile_2d',
                 system=dict(type='CC', evidence=['pressure plate'], evidence_status='explicit'),
                 article_database=[article('A1', 'pressure_plate'), article('A2', 'transom'),
                                   article(None, 'screw', 'piece', label='Screw', evidence_status='visual',
                                           inference_reason='drawn on centreline')],
                 assembly_database=[dict(assembly_id='a1', detail_id='d1', page=42, relation='fixed',
                                         participants=[dict(article='A1', role='fixed_part'), dict(article='A2', role='host')],
                                         fastener_assigned=dict(article_id=None, type='screw', passes_through=['A1'], engages_in='A2'),
                                         evidence=SRC, evidence_status='explicit')])
    value.update(kw)
    return value


def test_valid_detail_output_passes():
    out = validate_output(detail())
    assert out['assembly_database'][0]['fastener_assigned']['engages_in'] == 'A2'


def test_fastener_only_on_fixed_relation():
    bad = detail()
    bad['assembly_database'][0]['relation'] = 'clamped'
    with pytest.raises(ValueError, match="only allowed when relation is 'fixed'"):
        validate_output(bad)


def test_other_needs_clarification_and_visual_needs_reason():
    bad = detail()
    bad['assembly_database'][0]['relation'] = 'other'
    bad['assembly_database'][0]['fastener_assigned'] = None
    with pytest.raises(ValueError, match='relation_clarification'):
        validate_output(bad)
    bad = detail()
    bad['article_database'][2]['inference_reason'] = None
    with pytest.raises(ValueError, match='inference_reason'):
        validate_output(bad)


def test_semi_sg_needs_system_part_and_unknown_article_rejected():
    bad = detail(system=dict(type='semi-SG', evidence=['toggle', 'pressure plate'], evidence_status='explicit'))
    with pytest.raises(ValueError, match='system_part'):
        validate_output(bad)
    ok = deepcopy(bad)
    ok['assembly_database'][0]['system_part'] = 'CC'
    validate_output(ok)
    bad = detail()
    bad['assembly_database'][0]['participants'][1]['article'] = 'NOT_IN_DATABASE'
    with pytest.raises(ValueError, match='unknown article'):
        validate_output(bad)


def test_edge_spacing_keeps_printed_value_and_offset():
    value = detail()
    value['article_database'][2]['edge_spacing'] = dict(
        value=60, unit='mm', printed_value=50, offset_applied='added', evidence=SRC,
        edge_reference=dict(rail_end='x0', measured_from='dimension_line', offset=10, offset_unit='mm'))
    out = validate_output(value)
    assert out['article_database'][2]['edge_spacing']['printed_value'] == 50


def page(**rel):
    relation = dict(source='a', predicate='fastened_by', target='s', basis='drawing_reading', condition=None, bbox=BOX)
    relation.update(rel)
    return dict(pdf_page=1, page_kind='detail', details=[dict(id='d', title='t', drawing_refs=[], bbox=BOX)],
                nodes=[dict(id=i, detail_id='d', article_id=None, kind='part', label=i, material=None, quantity=None,
                            callout=None, bbox=BOX) for i in ('a', 's', 'm')],
                relations=[relation], measurements=[], tables=[], instructions=[], unresolved=[])


def test_stage1_relation_fastener_path_must_reference_nodes():
    assert validate(page(relation_type='fixed', passes_through=['a'], engages_in='m'), 1)
    with pytest.raises(ValueError, match='fastener path'):
        validate(page(relation_type='fixed', engages_in='missing'), 1)
    # A missing clarification never fails the region; it keeps the relationship out of the graph.
    from facade_dspy.exports import relationship_status
    relation = validate(page(relation_type='other'), 1)['relations'][0]
    node = dict(kind='part', bbox=BOX, detail_id='d')
    assert 'other_relation_without_clarification' in relationship_status(relation, node, node | dict(), False)[1]


def test_new_fields_are_optional_for_existing_stage1_output():
    assert validate(page(), 1)['relations'][0]['relation_type'] is None
    assert 'relation_type' in PageResult.model_json_schema()['$defs']['Relation']['properties']


def test_drawing_id_patterns_come_from_rules_not_schema(monkeypatch):
    value = page()
    value['nodes'][0]['article_id'] = 'XYZ-123'
    assert validate(value, 1)
    monkeypatch.setattr(rules, 'DRAWING_REFERENCE_PATTERNS', (r'XYZ-\d+',))
    with pytest.raises(ValueError, match='Drawing ID'):
        validate(value, 1)
