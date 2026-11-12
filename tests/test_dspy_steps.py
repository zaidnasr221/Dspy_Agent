"""DSPy steps of the classification process, offline: a fake backend answers, rewards decide."""
from copy import deepcopy
from pathlib import Path

from facade_dspy import dspy_steps
from facade_dspy.schema import validate_output

BOX = [.1, .1, .2, .2]


def output():
    src = [dict(page=7, kind='detail')]
    return validate_output(dict(
        catalogue='demo', page=7, detail_id='d7', title='Toggle elevation', view_type=None,
        system=dict(type='SG', evidence_status='explicit'),
        article_database=[
            dict(profile_id='T1', label='Toggle', profile_role='toggle', article_type='piece', evidence_status='explicit', sources=src),
            dict(profile_id='X9', label='Unknown insert', profile_role='other', role_clarification='insert',
                 article_type=None, evidence_status='explicit', sources=src),
            dict(profile_id='M1', label='Mullion', profile_role='mullion', article_type='bar', evidence_status='explicit', sources=src)],
        assembly_database=[dict(assembly_id='a1', detail_id='d7', page=7, relation='other', relation_clarification='toggle on mullion',
                                participants=[dict(article='T1', role='source'), dict(article='M1', role='target')],
                                evidence=[dict(page=7, kind='detail', text='toggle drawn in mullion groove')], evidence_status='visual',
                                inference_reason='drawn')]))


class Backend:
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def execute(self, task, prompt, schema, folder, heartbeat):
        self.calls.append(schema['title'])
        answer = self.answers[schema['title']]
        return dict(output=answer.pop(0) if isinstance(answer, list) else answer, raw_response=None)


GOOD = {
    'DrawingDecision': dict(view_type='assembly_orthographic', reason='elevation of a mullion with toggles'),
    'FWRDecision': dict(article_type='piece', reason='discrete insert placed at intervals'),
    'PieceSpacing': dict(edge_spacing=dict(printed_value=150, unit='mm', rail_end='x0', measured_from='dimension_line', offset=-10),
                         spacing=dict(value=300, unit='mm', bound='maximum'), evidence='150 max. 300 offset 10'),
    'RelationDecision': dict(relation='insertion', evidence='toggle clipped into mullion groove'),
}


def test_steps_fill_gaps_with_the_deliverable_vocabulary(tmp_path):
    backend = Backend(deepcopy(GOOD))
    (tmp_path / 'p7.png').write_bytes(b'png')
    done, log = dspy_steps.complete([output()], backend, tmp_path / 'steps', {7: tmp_path / 'p7.png'},
                                    {7: 'toggles: 150 max. 300 offset 10'})
    out = done[0]
    arts = {a['profile_id']: a for a in out['article_database']}
    assert out['view_type'] == 'assembly_orthographic'
    assert arts['X9']['article_type'] == 'piece'
    toggle = arts['T1']
    assert toggle['edge_spacing']['printed_value'] == 150 and toggle['edge_spacing']['value'] == 140
    assert toggle['edge_spacing']['offset_applied'] == 'subtracted' and toggle['spacing']['bound'] == 'maximum'
    assert toggle['evidence_status'] == 'visual' and toggle['inference_reason'].startswith('ExtractPieceSpacing')
    assert 'spacing' not in arts['M1'] or arts['M1']['spacing'] is None      # bars never get spacing
    assert out['assembly_database'][0]['relation'] == 'insertion'
    assert out['assembly_database'][0]['fastener_assigned'] is None
    assert all(item['accepted'] for item in log) and (tmp_path / 'steps').is_dir()


def test_unprinted_spacing_and_bad_relation_are_rejected(tmp_path):
    answers = deepcopy(GOOD)
    # A value not printed on the page (invented) and a fastener on a non-fixed relation: rewards are 0.
    answers['PieceSpacing'] = dict(spacing=dict(value=250, unit='mm'), evidence='typical spacing')
    answers['RelationDecision'] = dict(relation='clamped', fastener_type='screw', evidence='x')
    backend = Backend(answers)
    done, log = dspy_steps.complete([output()], backend, tmp_path / 'steps', {}, {7: 'toggles: 150 max. 300'})
    arts = {a['profile_id']: a for a in done[0]['article_database']}
    assert not arts['T1'].get('spacing') and arts['T1']['evidence_status'] == 'explicit'
    assert done[0]['assembly_database'][0]['relation'] == 'other'
    assert {i['step']: i['accepted'] for i in log}['ExtractPieceSpacing'] is False
    # BestOfN tried twice per piece (T1, and X9 once typed as a piece) before giving up.
    assert backend.calls.count('PieceSpacing') == 4


def test_fixed_relation_gets_fastener_type(tmp_path):
    answers = deepcopy(GOOD)
    answers['RelationDecision'] = dict(relation='fixed', fastener_type='screw', evidence='screw through toggle into mullion')
    done, _ = dspy_steps.complete([output()], Backend(answers), tmp_path / 'steps', {}, {7: 'toggles: 150 max. 300 offset 10'})
    item = done[0]['assembly_database'][0]
    assert item['relation'] == 'fixed' and item['fastener_assigned']['type'] == 'screw'


def test_rewards_are_deterministic():
    pred = type('P', (), {})()
    pred.reading = dspy_steps.PieceSpacing(spacing=dspy_steps.SpacingReading(value=300, unit='mm'), evidence='max 300')
    assert dspy_steps.spacing_reward(dict(piece=dict(page_text='Toggle max 300 c/c')), pred) == 1.0
    assert dspy_steps.spacing_reward(dict(piece=dict(page_text='nothing here')), pred) == 0.0
    pred.reading = dspy_steps.PieceSpacing(edge_spacing=dspy_steps.EdgeReading(printed_value=50, unit='mm', measured_from='dimension_line'),
                                          evidence='50')
    assert dspy_steps.spacing_reward(dict(piece=dict(page_text='50')), pred) == 0.0   # dimension line without its offset


def test_command_runs_on_a_saved_run_and_writes_the_deliverables(tmp_path, monkeypatch):
    import sys
    from facade_dspy import openai_api
    from facade_dspy.program import FacadeExtractionAgent
    from facade_dspy.storage import read
    from test_agent import pdf_file
    from test_relationship_pipeline import PipelineBackend
    pdf = pdf_file(tmp_path / 'fixture.pdf', pages=2)
    result = FacadeExtractionAgent(out=tmp_path / 'live', budget_usd=1,
                                   backend_factory=lambda *_: PipelineBackend([]))(pdf_url=pdf.as_uri())
    monkeypatch.setattr(openai_api, 'OpenAIResponses', lambda *a, **k: Backend(deepcopy(GOOD)))
    monkeypatch.setattr(sys, 'argv', ['dspy_steps', result.snapshot_directory, '--out', str(tmp_path / 'steps'),
                                      '--budget-usd', '0.5'])
    dspy_steps.main()
    out = tmp_path / 'steps'
    assert read(out / 'data/article_db.json')['articles'] and 'assemblies' in read(out / 'data/assembly_db.json')
    assert isinstance(read(out / 'dspy_steps_log.json'), list)
