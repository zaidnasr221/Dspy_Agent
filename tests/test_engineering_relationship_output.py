from copy import deepcopy
from pathlib import Path

from facade_dspy import articles, rules
from facade_dspy.contract_files import CONTRACTS
from facade_dspy.metric import score_detail
from facade_dspy.program import FacadeExtractionAgent
from facade_dspy.storage import read
from test_agent import pdf_file
from test_relationship_pipeline import PipelineBackend, cc_section, gold_detail, node, BOX, record


def test_classification_needs_glass_retention():
    nodes = [node('p', 'Pressure plate'), node('c', 'Cover cap'), node('g', 'Glass', kind='glass')]
    assert rules.detail_system(nodes)['type'] is None
    claims = [dict(source='g', target='p', predicate='clamped_by', basis='drawing_reading',
                   evidence='Glass clamped against plate', graph_eligible=True)]
    assert rules.detail_system(nodes, claims)['type'] == 'CC'


def test_variant_does_not_use_callout_as_a_size_and_preserves_fact_scope():
    base = record('235918', [42], label='235918')
    base['occurrences'][0]['callout'] = '14.5'
    source = dict(pdf_page=39, bbox=BOX)
    variant = record('235 918-14.5', [39], depth=[dict(value=10, unit='mm', provenance=source, bound='maximum')],
                     spacing=[dict(value=50, unit='mm', provenance=source, rail='other rail')])
    index = articles.build_index([base, variant])
    assert articles.link([base], index)[0][0]['link_status'] == 'variant_unresolved'
    base['occurrences'][0]['label'] = '235918 14.5(mm)'
    linked = articles.link([deepcopy(base)], index)[0][0]
    assert articles.linked_facts(linked, index, 'depth')[0]['provenance'] == source
    assert articles.linked_facts(linked, index, 'depth')[0]['bound'] == 'maximum'
    assert articles.linked_facts(linked, index, 'spacing') == []


def test_metric_penalizes_wrong_roles_order_and_evidence():
    gold = gold_detail()
    assembly = gold['assembly_database'][0]
    assembly['fastener_assigned']['passes_through'] = ['32374', 'Transom']
    assembly['evidence'][0]['bbox'] = [.1, .1, .2, .2]
    pred = deepcopy(gold)
    pred['assembly_database'][0]['participants'][0]['role'] = 'host'
    assert score_detail(gold, pred)['fields']['assemblies_recall'] == 0
    pred = deepcopy(gold)
    pred['assembly_database'][0]['fastener_assigned']['passes_through'].reverse()
    pred['assembly_database'][0]['evidence'][0]['bbox'] = [.7, .7, .8, .8]
    fields = score_detail(gold, pred)['fields']
    assert fields['fastener_assigned'] == fields['assembly_evidence'] == 0


def test_only_four_public_contracts():
    assert set(CONTRACTS) == {'page_result', 'article_record', 'assembly_record', 'detail_output'}


def test_rich_output_verification_and_replay(tmp_path):
    class Backend(PipelineBackend):
        def execute(self, task, prompt, schema, folder, heartbeat=lambda: None):
            reply = super().execute(task, prompt, schema, folder, heartbeat)
            if task == 'extraction' and reply['output']['extraction']['pdf_page'] == 1:
                page = cc_section()
                page['details'][0].update(view_type='assembly_section_2d', position='Transom')
                page['nodes'] += [node('upper', 'Upper glass', 'GL100', kind='glass'),
                                  node('lower', 'Lower glass', 'GL100', kind='glass'),
                                  node('plug', 'Balancing plug'), node('cleat', 'Cleat', '32380')]
                fixed, _, clamp = page['relations']
                fixed.update(fastener_position='centreline', distance_offsets=[
                    dict(value=11, unit='mm', from_datum='screw axis', to_datum=side,
                         evidence='11', bbox=BOX) for side in ('upper glass edge', 'lower glass edge')
                ])
                page['relations'].append(dict(fixed, engages_in=None, distance_offsets=None,
                                              fixing_evidence='alignment'))
                page['relations'].append(dict(fixed, condition='Alternate configuration', distance_offsets=None,
                                              evidence='Alternate fixing shown through the same plate'))
                clamp['participants'] = [dict(node_id=key, role=role) for key, role in
                                         [('pp','clamping_member'), ('gl','clamped_infill'),
                                          ('upper','clamped_infill'), ('lower','clamped_infill'),
                                          ('tr','host'), ('iso','isolator')]]
                page['measurements'].append(dict(subject='plug', property='quantity', value=8, unit=None,
                    datum=None, condition=None, bbox=BOX, evidence='Balancing plugs (8 per cleat)',
                    per='cleat', per_node_id='cleat'))
                reply['output']['extraction'] = page
                reply['output']['source_text'] += [dict(text='GL100 32380', bbox=BOX)]
            return reply

    pdf = pdf_file(tmp_path / 'fixture.pdf', pages=2)
    result = FacadeExtractionAgent(out=tmp_path / 'live', budget_usd=1, backend_factory=lambda *_: Backend([]))(
        pdf_url=pdf.as_uri())
    snapshot = Path(result.snapshot_directory)
    outputs = read(snapshot / 'data/detail_outputs.json')
    section = next(d for d in outputs if d['page'] == 1)
    assert section['view_type'] == 'assembly_section_2d' and section['position'] == 'Transom'
    assert section['system']['type'] == 'CC'
    assembly = section['assembly_database']
    clamp = next(a for a in assembly if a['relation'] == 'clamped')
    glasses = [p for p in clamp['participants'] if p['article'] == 'GL100']
    assert len(clamp['participants']) == 6 and len({p['occurrence_id'] for p in glasses}) == 2
    fixings = [a for a in assembly if a['relation'] == 'fixed']
    assert len(fixings) == 2
    assert all(a['fastener_assigned']['edge_spacing'] is None for a in fixings)
    assert all('article measurements not assigned to a specific fixing' in a['missing_information'] for a in fixings)
    fastener = next(a['fastener_assigned'] for a in fixings if a['fastener_assigned']['distance_offset'])
    assert [(m['value'], m['to_datum']) for m in fastener['distance_offset']] == [
        (11, 'upper glass edge'), (11, 'lower glass edge')]
    assert fastener['position'] == 'centreline'
    plug = next(a for a in section['article_database'] if a['label'] == 'Balancing plug')
    assert (plug['quantity']['value'], plug['quantity']['per'], plug['quantity']['per_article']) == (8, 'cleat', '32380')
    assert any('fixing_engagement_not_established' in issue for issue in section['unresolved'])
    inputs = next(Path(result.output_directory).glob('relation_inputs/*/relations.json'))
    assert any(len(r.get('participants') or []) == 6 for r in read(inputs))

    def forbidden(*_):
        raise AssertionError('Replay attempted a backend call')

    replay = FacadeExtractionAgent(out=tmp_path / 'replay', reuse=snapshot, replay=True,
                                   backend_factory=forbidden)(pdf_url=pdf.as_uri())
    assert read(Path(replay.snapshot_directory) / 'data/detail_outputs.json') == outputs
