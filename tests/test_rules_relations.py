"""Regressions for the generic rules: literal article IDs, identity keys, relationship statuses (offline only)."""
from pathlib import Path
import pytest

from facade_dspy import rules
from facade_dspy.exports import relationship_status
from facade_dspy.program import FacadeExtractionAgent, READING_RULES, RELATION_RULES
from facade_dspy.storage import read
from test_agent import FakeBackend, pdf_file


@pytest.mark.parametrize('value,kind', [
    ('A100', 'profile'), ('EM070', 'gasket'), ('030.6400.--', 'profile'), ('123 456', 'profile'),
    ('123 456', 'profile'), ('ALU 1234', 'bracket'), ('1234567', 'screw'), ('1234', 'cutting tool'),
    ('50SG02', 'gasket')])
def test_article_ids_are_kept(value, kind):
    assert rules.literal_article(value, kind)


@pytest.mark.parametrize('value,kind', [('K1013900', 'profile'), ('geffp12', 'profile'), ('1234', 'profile'),
                                        ('Gasket seal', 'gasket'), ('Transom', 'profile'), ('   ', 'profile'),
                                        (None, 'profile'), ('A100', 'dimension'), ('P001', 'position')])
def test_non_articles_are_not_components(value, kind):
    assert not rules.literal_article(value, kind)


def test_article_key_merges_digit_group_spacing_only():
    assert rules.article_key(' 123 456 ') == rules.article_key('123 456') == rules.article_key('123456') == '123456'
    assert rules.article_key('ALU 1234') == 'ALU 1234'
    assert rules.article_key('A100') != rules.article_key('a100')


def test_table_cells_report_everything_not_registered():
    assert rules.table_articles('A100 / 123 456') == (['A100', '123 456'], [], '')
    assert rules.table_articles('K1013900') == ([], ['K1013900'], '')
    assert rules.table_articles('see note 5') == ([], [], 'see note 5')
    assert rules.table_articles(None) == ([], [], '')


def test_identity_keys_ignore_case_spacing_and_legal_form():
    assert rules.identity_keys(dict(manufacturer_brand='Alupco LLC', system_id='50 SG')) == ('alupco', '50sg')
    assert rules.identity_keys(dict(manufacturer_brand='ALUPCO', system_id='50sg')) == ('alupco', '50sg')
    assert rules.identity_keys(dict(manufacturer_brand='Demo Brand', system_id='Demo:Facade')) == ('demo brand', 'demo:facade')
    assert rules.identity_keys({}) == (None, None)


def test_grounding_is_spacing_insensitive():
    assert rules.grounded('235918', ['Isolator 235 918 (20.5mm)'])
    assert rules.grounded('235 918', ['235918'])
    assert not rules.grounded('99999', ['Glazing gasket 235456'])


def test_prompts_carry_the_controlled_vocabulary_and_evidence_rule():
    for name in rules.PREDICATES:
        assert name in READING_RULES and name in RELATION_RULES
    assert 'evidence' in READING_RULES and 'TRANSCRIBE' in READING_RULES
    assert 'MANUFACTURER' not in READING_RULES


def node(kind='profile'):
    return dict(kind=kind)


def relation(**changes):
    return dict(dict(source='a', target='b', predicate='fixed_to', basis='drawing_reading',
                     evidence='screw callout pointing at bracket'), **changes)


@pytest.mark.parametrize('rel,source,target,disputed,expected', [
    (relation(), node(), node(), False, 'proposed'),
    (relation(basis='inference'), node(), node(), False, 'unresolved'),
    (relation(predicate='unresolved_relation'), node(), node(), False, 'unresolved'),
    (relation(evidence=None), node(), node(), False, 'unresolved'),
    (relation(), node('unresolved_reference'), node(), False, 'unresolved'),
    (relation(target='a'), node(), node(), False, 'invalid'),
    (relation(), node(), node('drawing_reference'), False, 'invalid'),
    (relation(), node(), node(), True, 'disputed'),
    (relation(basis='inference'), node(), node(), True, 'unresolved'),
])
def test_relationship_status(rel, source, target, disputed, expected):
    status, reasons = relationship_status(rel, source, target, disputed)
    assert status == expected
    assert bool(reasons) == (expected != 'proposed')


class Mixed(FakeBackend):
    """Adds an inferred relation, an unresolved subject and hard-to-parse article IDs."""

    def execute(self, task, prompt, schema, folder, heartbeat=lambda: None):
        reply = super().execute(task, prompt, schema, folder, heartbeat)
        if task == 'extraction':
            result = reply['output']['extraction']
            box = result['details'][0]['bbox']
            reply['output']['source_text'] = [dict(text='Gasket 030.6400.--', bbox=box)]
            result['nodes'].append(dict(id='g', detail_id='d', article_id='030.6400.--', kind='gasket', label='Gasket',
                                        material=None, quantity=None, callout=None, bbox=box))
            result['relations'] += [dict(source='a', predicate='seals_against', target='g', basis='inference', condition=None,
                                         bbox=box, evidence='gasket drawn against connector'),
                                    dict(source='ghost part', predicate='fixed_to', target='a', basis='drawing_reading',
                                         condition=None, bbox=box, evidence='leader')]
            result['tables'][0]['rows'].append(dict(cells=['K1013900 / see note 5', '1'], bbox=box))
        return reply


def test_export_keeps_ids_and_never_promotes_unresolved_relations(tmp_path):
    pdf = pdf_file(tmp_path / 'fixture.pdf')
    run = Path(FacadeExtractionAgent(out=tmp_path / 'runs', budget_usd=10,
                                     backend_factory=lambda r, b: Mixed([]))(pdf_url=pdf.as_uri()).output_directory)
    data = run / 'data'
    assert {c['article_id'] for c in read(data / 'components.json')} >= {'A100', 'V100', '030.6400.--'}
    relations = read(data / 'relationships.json')
    by_predicate = {}
    for r in relations:
        by_predicate.setdefault(r['predicate'], set()).add(r['relationship_status'])
        assert r['human_confirmed'] is False
        assert r['graph_eligible'] == (r['relationship_status'] == 'proposed')
    assert by_predicate['fastened_by'] == {'proposed'}
    assert by_predicate['seals_against'] == {'unresolved'}
    assert by_predicate['fixed_to'] == {'unresolved'}
    categories = {q['category'] for q in read(data / 'questions.json')}
    assert {'relationship_validation', 'drawing_reference_in_article_column', 'unparsed_table_article_cell'} <= categories
    assert 'manufacturer_profile' not in read(run / 'document.json')


def test_unsupported_verdict_removes_relationship_from_graph(tmp_path):
    pdf = pdf_file(tmp_path / 'fixture.pdf')
    calls = []
    run = Path(FacadeExtractionAgent(out=tmp_path / 'runs', budget_usd=10,
               backend_factory=lambda r, b: FakeBackend(calls, 'relation_unsupported'))(pdf_url=pdf.as_uri()).output_directory)
    relations = read(run / 'data/relationships.json')
    assert relations and all(r['relationship_status'] == 'invalid' and r['verification'] == 'unsupported' for r in relations)
    assert read(run / 'data/graph_relationships.json') == []
    assert calls.count('relation_check') == 2


def test_simple_brand_keeps_previous_component_keys(tmp_path):
    from facade_dspy.exports import canonical_id
    pdf = pdf_file(tmp_path / 'fixture.pdf')
    run = Path(FacadeExtractionAgent(out=tmp_path / 'runs', budget_usd=10,
                                     backend_factory=lambda r, b: FakeBackend([]))(pdf_url=pdf.as_uri()).output_directory)
    components = {c['article_id']: c for c in read(run / 'data/components.json')}
    # Same formula and inputs as before: case-folded brand and system for names without spaces or legal suffixes.
    assert components['A100']['canonical_id'] == canonical_id('component', 'system', 'demo', 'demo:facade', 'A100')
    assert components['A100']['component_id'] == 'demo:demo:facade:A100'
