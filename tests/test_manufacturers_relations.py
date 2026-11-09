"""Regressions: literal article IDs, relationship statuses and manufacturer profiles (offline only)."""
import json
from pathlib import Path
import pytest

from facade_dspy import manufacturers
from facade_dspy.exports import relationship_status
from facade_dspy.program import FacadeExtractionAgent
from facade_dspy.schema import validate
from facade_dspy.storage import read
from test_agent import FakeBackend, page_result, pdf_file


@pytest.fixture
def default():
    return manufacturers.resolve({})['profile']


@pytest.fixture
def profile_dir(tmp_path, monkeypatch):
    folder = tmp_path / 'profiles'
    folder.mkdir()
    monkeypatch.setenv('FACADE_MANUFACTURER_DIR', str(folder))
    manufacturers._load.cache_clear()
    yield folder
    manufacturers._load.cache_clear()


@pytest.mark.parametrize('value,kind', [
    ('A100', 'profile'), ('EM070', 'gasket'), ('030.6400.--', 'profile'), ('123 456', 'profile'),
    ('123 456', 'profile'), ('ALU 1234', 'bracket'), ('1234567', 'screw'), ('1234', 'cutting tool')])
def test_previously_lost_article_ids_are_kept(value, kind, default):
    assert manufacturers.literal_article(value, kind, default)


@pytest.mark.parametrize('value,kind', [('K1013900', 'profile'), ('geffp12', 'profile'), ('1234', 'profile'),
                                        ('Gasket seal', 'gasket'), ('   ', 'profile'), (None, 'profile')])
def test_non_articles_are_not_components(value, kind, default):
    assert not manufacturers.literal_article(value, kind, default)


def test_article_key_unifies_spacing_only(default):
    assert manufacturers.article_key(' 123 456 ') == manufacturers.article_key('123 456') == '123 456'
    assert manufacturers.article_key('A100') != manufacturers.article_key('a100')


def test_table_cells_report_everything_not_registered(default):
    assert manufacturers.table_articles('A100 / 123 456', default) == (['A100', '123 456'], [], '')
    assert manufacturers.table_articles('K1013900', default) == ([], ['K1013900'], '')
    assert manufacturers.table_articles('see note 5', default) == ([], [], 'see note 5')
    assert manufacturers.table_articles(None, default) == ([], [], '')


def node(kind='profile'):
    return dict(kind=kind)


def relation(**changes):
    return dict(dict(source='a', target='b', predicate='fixed_to', basis='drawing_reading'), **changes)


@pytest.mark.parametrize('rel,source,target,disputed,expected', [
    (relation(), node(), node(), False, 'proposed'),
    (relation(basis='inference'), node(), node(), False, 'unresolved'),
    (relation(predicate='unresolved_relation'), node(), node(), False, 'unresolved'),
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


def test_brand_aliases_share_one_scope_and_unknown_brands_are_unchanged():
    a = manufacturers.resolve(dict(manufacturer_brand='Alupco LLC', system_id='50SG'))
    b = manufacturers.resolve(dict(manufacturer_brand='ALUPCO', system_id='50sg'))
    c = manufacturers.resolve(dict(manufacturer_brand='Other', legal_manufacturer='Aluminium Products Company', system_id='X'))
    assert a['brand_key'] == b['brand_key'] == c['brand_key'] == 'alupco' and a['system_key'] == b['system_key']
    legacy = manufacturers.resolve(dict(manufacturer_brand='Demo Brand', system_id='Demo:Facade'))
    assert (legacy['profile_id'], legacy['brand_key'], legacy['system_key']) == ('default', 'demo brand', 'demo:facade')


def test_custom_profile_overrides_rules_without_code(profile_dir):
    (profile_dir / 'acme.json').write_text(json.dumps(dict(
        id='acme', brand_aliases=['ACME Facades'], system_aliases={'CW 60': ['CW60', 'Curtain Wall 60']},
        drawing_reference_patterns=['DRG-\\d+'], article_patterns=['AC-\\d{4}'])), encoding='utf-8')
    scope = manufacturers.resolve(dict(manufacturer_brand='acme facades', system_id='Curtain Wall 60'))
    assert (scope['profile_id'], scope['brand_key'], scope['system_key']) == ('acme', 'acme', 'cw 60')
    profile = scope['profile']
    assert manufacturers.literal_article('AC-1234', 'profile', profile)
    assert not manufacturers.literal_article('A100', 'profile', profile)
    assert not manufacturers.literal_article('DRG-12', 'profile', profile)
    assert profile['tool_kind_keywords'] == ['tool', 'jig']  # inherited from default.json
    result = page_result(1)
    result['nodes'][0]['article_id'] = 'DRG-12'
    with pytest.raises(ValueError, match='Drawing ID'):
        validate(result, 1, profile['drawing_reference_patterns'])
    result['nodes'][0]['article_id'] = 'K1013900'
    assert validate(result, 1, profile['drawing_reference_patterns'])


def test_conflicting_aliases_are_rejected(profile_dir):
    (profile_dir / 'copy.json').write_text(json.dumps(dict(id='copy', brand_aliases=['ALUPCO'])), encoding='utf-8')
    with pytest.raises(ValueError, match='claimed by'):
        manufacturers.resolve(dict(manufacturer_brand='ALUPCO'))


class Mixed(FakeBackend):
    """Adds an inferred relation, an unresolved subject and hard-to-parse article IDs."""

    def execute(self, task, prompt, schema, folder, heartbeat=lambda: None):
        reply = super().execute(task, prompt, schema, folder, heartbeat)
        if task == 'extraction':
            result = reply['output']['extraction']
            box = result['details'][0]['bbox']
            result['nodes'].append(dict(id='g', detail_id='d', article_id='030.6400.--', kind='gasket', label='Gasket',
                                        material=None, quantity=None, callout=None, bbox=box))
            result['relations'] += [dict(source='a', predicate='seals_against', target='g', basis='inference', condition=None, bbox=box),
                                    dict(source='ghost part', predicate='fixed_to', target='a', basis='drawing_reading', condition=None, bbox=box)]
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
    assert read(run / 'document.json')['manufacturer_profile'] == 'default'


def test_unmatched_brand_keeps_previous_component_keys(tmp_path):
    from facade_dspy.exports import canonical_id
    pdf = pdf_file(tmp_path / 'fixture.pdf')
    run = Path(FacadeExtractionAgent(out=tmp_path / 'runs', budget_usd=10,
                                     backend_factory=lambda r, b: FakeBackend([]))(pdf_url=pdf.as_uri()).output_directory)
    components = {c['article_id']: c for c in read(run / 'data/components.json')}
    # Same formula and inputs as before profiles existed: brand.casefold(), system.casefold().
    assert components['A100']['canonical_id'] == canonical_id('component', 'system', 'demo', 'demo:facade', 'A100')
    assert components['A100']['component_id'] == 'demo:demo:facade:A100'
