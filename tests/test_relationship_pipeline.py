"""New pipeline pieces, offline: knowledge-driven system type, article index and cross-page linking,
explicit references, per-detail ARTICLE/ASSEMBLY outputs, JSON contracts and the gold metric."""
import json
from copy import deepcopy
from pathlib import Path
from typing import get_args

import pytest

from facade_dspy import articles, references, rules
from facade_dspy.contract_files import CONTRACTS, render
from facade_dspy.metric import score, score_detail
from facade_dspy.program import FacadeExtractionAgent, READING_RULES
from facade_dspy.schema import ProfileRole, validate_output
from facade_dspy.storage import read
from test_agent import FakeBackend, pdf_file

ROOT = Path(__file__).resolve().parents[1]


# --- knowledge and system type ----------------------------------------------------------------

def test_knowledge_roles_match_the_output_contract_and_prompt():
    families = set(rules.knowledge('system_profiles')['families'])
    assert families | {'other'} == set(get_args(ProfileRole))
    assert all(name in READING_RULES for name in families)


@pytest.mark.parametrize('node, expected', [
    (dict(kind='component', label='Pressure plate'), 'pressure_plate'),
    (dict(kind='sealant', label='Structural silicone'), 'structural_silicone'),
    (dict(kind='sealant', label='silicone'), None),               # role, not name, decides
    (dict(kind='gasket', label='Gasket'), None),
    (dict(kind='fastener', label='Fixture screw'), 'screw'),
    (dict(kind='x', label='anything', profile_role='toggle'), 'toggle'),
])
def test_family_is_generic_and_by_role(node, expected):
    assert rules.family(node) == expected


@pytest.mark.parametrize('labels, expected', [
    (['Mullion', 'Pressure plate', 'Cover cap', 'screw'], None),
    (['Mullion', 'Structural silicone', 'Toggle', 'glass'], None),
    (['Mullion', 'Structural silicone', 'Pressure plate'], None),
    (['Mullion', 'glass', 'silicone'], None),
])
def test_system_type_is_unresolved_from_part_names_alone(labels, expected):
    nodes = [dict(kind='part', label=x) for x in labels]
    assert rules.detail_system(nodes)['type'] == expected


def test_declared_system_type_is_checked_not_trusted():
    nodes = [dict(id='p', kind='part', label='Pressure plate'),dict(id='g', kind='glass', label='Glass')]
    claims = [dict(source='g',target='p',predicate='clamped_by',basis='explicit_text',
                   evidence='Pressure plate holds glass',graph_eligible=True)]
    computed, issues = rules.system_issues('SG', nodes, claims)
    assert computed['type'] == 'CC' and issues[0]['issue'] == 'system_type_mismatch'
    assert rules.system_issues('CC', [dict(kind='part', label='glass')])[1][0]['issue'] == 'system_type_without_retention_evidence'


def test_assembly_issues():
    nodes = {'p': dict(kind='part', label='Pressure plate'), 'm': dict(kind='part', label='Mullion'),
             's': dict(kind='fastener', label='screw'), 't': dict(kind='part', label='Toggle')}
    assert rules.assembly_issues(dict(relation_type='fixed', source='p', target='m'), nodes) == ['fixed_without_fastener']
    assert 'fastener_id_not_a_fastener' in rules.assembly_issues(dict(relation_type='fixed', source='p', target='m', fastener_id='m'), nodes)
    assert rules.assembly_issues(dict(relation_type='fixed', source='p', target='m', fastener_id='s'), nodes) == []
    assert 'system_part_mismatch' in rules.assembly_issues(dict(source='p', target='m', system_part='SG'), nodes)


def test_drawing_reference_conventions_are_data():
    conventions = json.loads((ROOT / 'facade_dspy/knowledge/catalogue_conventions.json').read_text(encoding='utf-8'))
    assert rules.DRAWING_REFERENCE_PATTERNS == tuple(conventions['drawing_reference_patterns'])
    assert 'K\\d{7}' not in (ROOT / 'facade_dspy/rules.py').read_text(encoding='utf-8')
    assert 'geff' not in (ROOT / 'facade_dspy/schema.py').read_text(encoding='utf-8')


# --- article index and cross-page links ---------------------------------------------------------

def record(pid, pages, label='', record_id=None, **fields):
    occ = [dict(label=label, callout=None, canonical_id=f'{pid}:{p}', canonical_detail_id=f'd{p}',
                provenance=dict(pdf_page=p, bbox=[.1, .1, .2, .2])) for p in pages]
    return dict(profile_id=pid, record_id=record_id or pid, occurrences=occ, table_observations=[], **fields)


def test_article_base_and_variant():
    assert articles.article_base('235 918-14.5') == ('235918', '14.5')
    assert articles.article_base('32374') == ('32374', None)


def test_base_id_links_to_variant_only_with_printed_size():
    db = [record('235918', [42], label='235918 14.5(mm)'),
          record('235 918-14.5', [39], label='Isolator 14.5mm', depth=[dict(value=10, unit='mm', provenance=dict(pdf_page=39))]),
          record('235 918-11.4', [39], label='Isolator 11.5mm')]
    index = articles.build_index(db)
    assert index['235918']['variants'] == ['235918-11.4', '235918-14.5']
    linked, issues = articles.link(deepcopy(db), index)
    assert linked[0]['link_status'] == 'linked_variant' and linked[0]['linked'][0]['article_key'] == '235918-14.5'
    assert articles.linked_facts(linked[0], index, 'depth')[0]['value'] == 10 and not issues
    db[0]['occurrences'][0]['label'] = '235918'
    unresolved, issues = articles.link(deepcopy(db), index)
    assert unresolved[0]['link_status'] == 'variant_unresolved' and issues[0]['category'] == 'article_variant_unresolved'


def test_same_article_on_two_pages_is_linked_with_pages():
    db = [record('32374', [42, 71])]
    index = articles.build_index(db)
    assert index['32374']['seen_on'] == [42, 71]


# --- explicit references ------------------------------------------------------------------------

def ref_inputs(text, page=49, printed=None):
    prov = dict(pdf_page=page, bbox=[0, 0, 1, 1])
    details = [dict(canonical_id='d49', title='Cleats isometric', drawing_refs=[], provenance=prov),
               dict(canonical_id='d50', title='Section-1 cleat fixing', drawing_refs=[], provenance=dict(pdf_page=50, bbox=[0, 0, 1, 1]))]
    instructions = [dict(canonical_detail_id='d49', text=text, provenance=prov)]
    index = [dict(pdf_page=n, printed_page=(printed or {}).get(n)) for n in range(1, 76)]
    return details, instructions, [], [], [], [], index


def test_references_resolve_relative_and_printed_pages():
    links = references.find(*ref_inputs('Refer next page for section-1'))
    assert links[0]['target_pages'] == [50] and links[0]['status'] == 'resolved'
    links = references.find(*ref_inputs('See page 12', printed={60: '12'}))
    assert links[0]['target_pages'] == [60]
    assert references.find(*ref_inputs('See page 300'))[0]['status'] == 'unresolved'


def test_page_headers_are_not_references():
    assert references.find(*ref_inputs('Page 1 of the catalogue')) == []


# --- contracts and metric -----------------------------------------------------------------------

def test_schema_files_are_in_sync_with_models():
    for name, model in CONTRACTS.items():
        path = ROOT / 'schemas' / f'{name}.schema.json'
        assert json.loads(path.read_text(encoding='utf-8')) == json.loads(render(model)), name


def gold_detail():
    src = [dict(page=42, kind='detail')]
    return validate_output(dict(
        catalogue='demo', page=42, detail_id='d', title='Transom', system=dict(type='CC', evidence_status='explicit'),
        article_database=[
            dict(profile_id='32374', profile_role='pressure_plate', article_type='bar', evidence_status='explicit', sources=src,
                 depth=dict(value=11, unit='mm')),
            dict(profile_id=None, label='Transom', profile_role='transom', article_type='bar', evidence_status='explicit', sources=src),
            dict(profile_id=None, label='screw', profile_role='screw', article_type='piece', evidence_status='visual',
                 inference_reason='drawn', sources=src)],
        assembly_database=[dict(assembly_id='a', detail_id='d', page=42, relation='fixed', evidence=src, evidence_status='visual',
                                inference_reason='drawn', participants=[dict(article='32374', role='fixed_part'), dict(article='Transom', role='host')],
                                fastener_assigned=dict(article_id=None, type='screw', engages_in='Transom'))]))


def test_metric_rewards_exact_and_zeroes_invented_values():
    gold = gold_detail()
    assert score_detail(gold, deepcopy(gold))['score'] == 1.0
    pred = deepcopy(gold)
    pred['article_database'][0]['depth']['value'] = 12.0
    assert 0 < score_detail(gold, pred)['score'] < 1
    pred = deepcopy(gold)
    pred['article_database'][1]['depth'] = dict(value=150.0, unit='mm', evidence=[])
    report = score_detail(gold, pred)
    assert report['score'] == 0.0 and report['invented']
    assert score([gold], [])['score'] == 0.0


# --- end to end: mock backend, no API -----------------------------------------------------------

BOX = [.1, .1, .6, .6]


def node(i, label, article=None, kind='part', **kw):
    return dict(dict(id=i, detail_id='d', article_id=article, kind=kind, label=label, material=None, quantity=None,
                     callout=article, bbox=BOX), **kw)


def cc_section():
    nodes = [node('pp', 'Pressure plate', 'PP100', article_type='bar'), node('cc', 'Cover cap', 'CC200'),
             node('iso', '235918 14.5(mm)', '235918', kind='isolator'), node('tr', 'Transom'),
             node('sc', 'screw', kind='fastener'), node('gl', 'glass', kind='glass')]
    relations = [
        dict(source='pp', predicate='fixed_to', target='tr', basis='drawing_reading', condition=None, bbox=BOX,
             evidence='screw drawn through pressure plate and isolator into transom', relation_type='fixed',
             fastener_id='sc', passes_through=['pp', 'iso'], engages_in='tr', insertion_direction='exterior_to_interior'),
        dict(source='cc', predicate='clipped_into', target='pp', basis='drawing_reading', condition=None, bbox=BOX,
             evidence='cover cap legs engaged on pressure plate', relation_type='insertion'),
        dict(source='gl', predicate='clamped_by', target='pp', basis='drawing_reading', condition=None, bbox=BOX,
             evidence='glass edge between gasket and pressure plate', relation_type='clamped')]
    measurements = [dict(subject='pp', property='spacing', value=300, unit='mm', datum=None, condition=None, bbox=BOX,
                         evidence='max 300 c/c', bound='maximum'),
                    dict(subject='sc', property='edge_spacing', value=50, unit='mm', datum=None, condition=None, bbox=BOX,
                         evidence='50 to dimension line', edge_reference=dict(rail_end='x0', measured_from='dimension_line',
                                                                              offset=-5, offset_unit='mm'))]
    return dict(pdf_page=1, page_kind='detail',
                details=[dict(id='d', title='Transom section', drawing_refs=[], bbox=BOX, system_type='CC', view_type='profile_2d')],
                nodes=nodes, relations=relations, measurements=measurements, tables=[],
                instructions=[dict(detail_id='d', sequence=None, text='Refer next page for isolator sizes', condition=None,
                                   article_refs=['235918'], bbox=BOX)], unresolved=[])


def table_page():
    rows = [dict(cells=['235 918-14.5', 'Isolator 14.5mm', '10'], bbox=BOX), dict(cells=['235 918-11.4', 'Isolator 11.5mm', '10'], bbox=BOX),
            dict(cells=['PP100', 'Pressure plate', '11'], bbox=BOX)]
    return dict(pdf_page=2, page_kind='table', details=[dict(id='d', title='Profile table', drawing_refs=[], bbox=BOX, view_type='table')],
                nodes=[], relations=[], measurements=[], instructions=[], unresolved=[],
                tables=[dict(id='t', title='Profiles', columns=['Art.No.', 'Description', 'Depth'], units=[None, None, 'mm'],
                             rows=rows, footnotes=[], bbox=BOX)])


class PipelineBackend(FakeBackend):
    def execute(self, task, prompt, schema, folder, heartbeat=lambda: None):
        reply = super().execute(task, prompt, schema, folder, heartbeat)
        if task == 'identity':
            reply['output']['evidence_pages'] = [1]
        if task == 'extraction':
            unit = read(folder / 'scope.json')
            page = cc_section() if unit['pdf_page'] == 1 else table_page()
            texts = ['PP100', 'CC200', '235918', '235 918-14.5', '235 918-11.4']
            reply['output'] = dict(unit_id=unit['unit_id'], extraction=page,
                                   source_text=[dict(text=t, bbox=BOX) for t in texts])
        return reply


def test_end_to_end_detail_outputs_without_api(tmp_path):
    pdf = pdf_file(tmp_path / 'fixture.pdf', pages=2)
    calls = []
    result = FacadeExtractionAgent(out=tmp_path / 'live', budget_usd=1,
                                   backend_factory=lambda *_: PipelineBackend(calls))(pdf_url=pdf.as_uri())
    snap = Path(result.snapshot_directory)
    outputs = read(snap / 'data/detail_outputs.json')
    section = next(o for o in outputs if o['page'] == 1)
    assert validate_output(section) and (snap / 'data/details').is_dir()
    assert section['system']['type'] == 'CC' and section['system']['evidence_status'] == 'visual'
    arts = {a['profile_id'] or a['label']: a for a in section['article_database']}
    assert arts['PP100']['profile_role'] == 'pressure_plate' and arts['PP100']['spacing']['value'] == 300.0
    assert arts['PP100']['spacing']['bound'] == 'maximum'
    # Cross-page: the pressure plate's table row on page 2 is attached with its page.
    assert arts['PP100']['other']['depth']['value'] == '11' and 2 in {s['page'] for s in arts['PP100']['sources']}
    # Callout 235918 + printed 14.5 links to variant 235 918-14.5 on page 2, not to 11.4.
    iso = arts['235918']
    assert any('235918-14.5' in (s['text'] or '') for s in iso['sources'])
    screw = arts['screw']
    assert screw['edge_spacing']['printed_value'] == 50 and screw['edge_spacing']['value'] == 45
    assert screw['edge_spacing']['offset_applied'] == 'subtracted' and screw['evidence_status'] == 'visual'
    fixed = next(a for a in section['assembly_database'] if a['relation'] == 'fixed')
    assert fixed['fastener_assigned']['type'] == 'screw'
    assert fixed['fastener_assigned']['passes_through'] == ['PP100', '235918']
    assert fixed['fastener_assigned']['engages_in'] == 'Transom'
    assert {p['article'] for p in fixed['participants']} == {'PP100', 'Transom'}
    assert all(a['fastener_assigned'] is None for a in section['assembly_database'] if a['relation'] != 'fixed')
    assert {a['relation'] for a in section['assembly_database']} == {'fixed', 'insertion', 'clamped'}
    links = read(snap / 'data/reference_links.json')
    assert any(l['target_pages'] == [2] and l['status'] == 'resolved' for l in links)
    index = read(snap / 'data/article_index.json')
    assert index['235918']['variants'] == ['235918-11.4', '235918-14.5']
    # Replay rebuilds the same outputs without any backend.
    def forbidden(*_):
        raise AssertionError('replay used a backend')
    replay = FacadeExtractionAgent(out=tmp_path / 'replay', reuse=snap, replay=True, backend_factory=forbidden)(pdf_url=pdf.as_uri())
    assert read(Path(replay.snapshot_directory) / 'data/detail_outputs.json') == outputs


def test_schemas_folder_has_only_the_published_contracts():
    assert sorted(p.name for p in (ROOT / 'schemas').glob('*.schema.json')) == sorted(f'{n}.schema.json' for n in CONTRACTS)


def test_prompts_carry_the_output_contract():
    from facade_dspy.program import RELATION_RULES, REVIEW_RULES
    for word in ('system_type', 'semi-SG', 'profile_role', 'article_type', 'relation_type', 'passes_through',
                 'engages_in', 'edge_reference', 'FIXINGS', 'page_kind', 'Never measure'):
        assert word in READING_RULES, word
    for word in ('system_type', 'profile_role', 'edge_spacing', 'engages'):
        assert word in REVIEW_RULES, word
    assert 'leader line' in RELATION_RULES and 'fastener path' in RELATION_RULES
    for prompt in (READING_RULES, REVIEW_RULES, RELATION_RULES):
        assert 'ALUPCO' not in prompt and 'Technal' not in prompt and 'K\\d{7}' not in prompt


def test_fixed_to_with_fastener_is_one_fixing_relationship():
    from facade_dspy.exports import relationship_status
    nodes = dict(p=dict(kind='part', label='Pressure plate'), t=dict(kind='part', label='Transom'))
    rel = dict(source='p', predicate='fixed_to', target='t', basis='drawing_reading', evidence='screw drawn',
               bbox=[.1, .1, .2, .2], fastener_id='p')
    assert 'fastener_is_an_endpoint_of_its_own_fixing' in relationship_status(rel, nodes['p'], nodes['t'], False)[1]


def test_cleat_and_glass_support_are_typical_for_both_systems():
    systems = rules.knowledge('system_profiles')['systems']
    for name in ('CC', 'SG'):
        assert {'cleat', 'glass_support'} <= set(systems[name]['expected'])
    assert 'cleat' in READING_RULES and 'never a reason to add one' in READING_RULES


def test_semi_sg_is_split_by_axis():
    nodes = [dict(kind='part', label=x) for x in ('Mullion', 'Transom')]
    assert rules.axis_of(nodes[:1]) == 'vertical' and rules.axis_of(nodes[1:]) == 'horizontal'
    assert rules.axis_of(nodes) is None
    assert rules.semi_axes({'vertical': {'SG'}, 'horizontal': {'CC'}}) == ({'vertical': 'SG', 'horizontal': 'CC'}, [])
    assert rules.semi_axes({'vertical': {'SG', 'CC'}})[1] == ['semi_sg_both_systems_on_vertical_axis']
    assert rules.semi_axes({'vertical': {'CC'}, 'horizontal': {'CC'}})[1] == ['semi_sg_same_system_on_both_axes']


def semi_section():
    page = cc_section()
    page['nodes'] += [node('mu', 'Mullion'), node('ss', 'Structural silicone', kind='sealant'),
                      node('gs', 'Glass support', kind='setting block')]
    page['details'][0]['system_type'] = 'semi-SG'
    page['relations'] += [
        dict(source='gl', predicate='bonded_to', target='mu', basis='drawing_reading', condition=None, bbox=BOX,
             evidence='structural silicone bead between glass and mullion', relation_type='bonded',
             participants=[dict(node_id=n,role=r) for n,r in [('gl','infill'),('mu','host'),('ss','structural_bond')]]),
        dict(source='ss', predicate='bonded_to', target='mu', basis='drawing_reading', condition=None, bbox=BOX,
             evidence='silicone bead on mullion face', relation_type='bonded'),
        dict(source='gs', predicate='supports', target='gl', basis='drawing_reading', condition=None, bbox=BOX,
             evidence='glass support under glass at transom', relation_type='supported')]
    return page


def test_semi_sg_detail_gets_per_axis_systems_and_parts(tmp_path):
    class SemiBackend(PipelineBackend):
        def execute(self, task, prompt, schema, folder, heartbeat=lambda: None):
            reply = super().execute(task, prompt, schema, folder, heartbeat)
            if task == 'extraction' and reply['output']['extraction']['pdf_page'] == 1:
                reply['output']['extraction'] = semi_section()
            return reply
    pdf = pdf_file(tmp_path / 'fixture.pdf', pages=2)
    result = FacadeExtractionAgent(out=tmp_path / 'live', budget_usd=1,
                                   backend_factory=lambda *_: SemiBackend([]))(pdf_url=pdf.as_uri())
    section = next(o for o in read(Path(result.snapshot_directory) / 'data/detail_outputs.json') if o['page'] == 1)
    assert section['system']['type'] == 'semi-SG'
    assert section['system']['per_axis'] == {'horizontal': 'CC', 'vertical': 'SG'}
    parts = {a['relation']: a['system_part'] for a in section['assembly_database']}
    assert parts['fixed'] == 'CC' and parts['bonded'] == 'SG'
    # The glass support relation has no system-only part; it is not guessed (glass touches both axes).
    assert all(a['system_part'] for a in section['assembly_database'])


def test_output_folder_has_only_the_needed_files(tmp_path):
    pdf = pdf_file(tmp_path / 'fixture.pdf', pages=2)
    result = FacadeExtractionAgent(out=tmp_path / 'live', budget_usd=1,
                                   backend_factory=lambda *_: PipelineBackend([]))(pdf_url=pdf.as_uri())
    data = Path(result.snapshot_directory) / 'data'
    names = {p.name for p in data.iterdir() if p.is_file()}
    assert not [n for n in names if n.endswith(('.csv', '.parquet'))]
    assert {'detail_outputs.json', 'article_database.json', 'assembly_database.json', 'article_index.json',
            'reference_links.json', 'questions.json'} <= names
    for dropped in ('drawing_instances', 'connections', 'machining_features', 'tools', 'consumables', 'source_text',
                    'source_crops', 'reference_index', 'conflicts', 'cross_document_links'):
        assert f'{dropped}.json' not in names
    assert (data / 'details').is_dir()
