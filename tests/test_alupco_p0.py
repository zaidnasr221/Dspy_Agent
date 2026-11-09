"""P0 regressions from the ALUPCO 50 SG catalogue (data/50SG ALUPCO PDF COMBINED.pdf).

Values are transcribed from physical page 15 ("Rubber gaskets & isolators" table and the
transom section callouts) and page 13 (installation positions 1-7). The pages have no
embedded text, so the strings below are what a visual reader returns. Offline only.
"""
from pathlib import Path
import pytest

from facade_dspy import manufacturers
from facade_dspy.exports import relationship_status
from facade_dspy.program import FacadeExtractionAgent
from facade_dspy.storage import read
from test_agent import FakeBackend, pdf_file

ALUPCO = dict(manufacturer_brand='ALUPCO', legal_manufacturer=None, system_id='50 SG',
              document_name='4-Sided Structural Glazing 50 SG', edition=None, copyright_notice=None,
              evidence_pages=[1], unresolved=[])


@pytest.fixture
def alupco():
    return manufacturers.resolve(ALUPCO)['profile']


# --- 1. Table article ID tokenization -------------------------------------------------

@pytest.mark.parametrize('cell,expected', [
    ('Art.-Nr. 235 918-11.5 (Isolator 11.4mm)', ['235 918-11.5']),
    ('Art.-Nr. 235 918-14.5 (Isolator 14.5mm)', ['235 918-14.5']),
    ('Art.-Nr. 235 918-18.5 (Isolator 18.5mm)', ['235 918-18.5']),
    ('Art.-Nr. 235 918-20.5 (Isolator 20.5mm)', ['235 918-20.5']),
    ('Art.-Nr. 235 545', ['235 545']),
    ('Art.-Nr. 235 546', ['235 546']),
    ('Art.-Nr. 235 547', ['235 547']),
    ('Art.-Nr. 237 351', ['237 351']),
    ('235918 (20.5mm)', ['235918']),
    ('235456', ['235456']),
    ('32476', ['32476']),
    ('32478', ['32478']),
    ('32477', ['32477']),
    ('235917', ['235917']),
])
def test_page15_article_cells_keep_complete_ids(cell, expected, alupco):
    found, drawings, leftover = manufacturers.table_articles(cell, alupco)
    assert (found, drawings, leftover) == (expected, [], '')


@pytest.mark.parametrize('cell', ['11.4', '14.5', '18.5', '20.5', '(Isolator 11.4mm)', '5 - 7 mm', '9 - 11 mm',
                                  '11 - 13 mm', 'Inner glazing gasket 7 - 9 mm', 'Isolator 20.5mm'])
def test_page15_dimensions_never_become_components_or_questions(cell, alupco):
    assert manufacturers.table_articles(cell, alupco) == ([], [], '')
    assert not manufacturers.literal_article(cell.strip('()'), 'isolator', alupco)


@pytest.mark.parametrize('cell', ['34', '22', '6'])
def test_short_numbers_in_article_column_are_questions_not_components(cell, alupco):
    # Dimension-chain values (page 15: 34, 22, 6) are not IDs, but nothing in an article column is dropped silently.
    assert manufacturers.table_articles(cell, alupco) == ([], [], cell)


def test_variant_suffix_is_never_split_into_a_second_component(alupco):
    found, _, _ = manufacturers.table_articles('235 918-11.5 / 235 918-20.5', alupco)
    assert found == ['235 918-11.5', '235 918-20.5']
    assert '11.5' not in found and '20.5' not in found


def test_alupco_spellings_share_a_key_but_variants_stay_distinct(alupco):
    key = lambda v: manufacturers.article_key(v, alupco)
    assert key('235 918') == key('235918') == key('235 918') == '235918'
    assert len({key('235918'), key('235 918-11.5'), key('235 918-20.5')}) == 3
    # Other manufacturers keep spaced IDs literal unless their profile opts in.
    default = manufacturers.resolve({})['profile']
    assert manufacturers.article_key('235 918', default) == '235 918'


# --- 2. Strict relationship eligibility -----------------------------------------------

TOP = [0, 0, 1, .5]


def n(kind, box=(.3, .2, .4, .3), detail='transom'):
    return dict(kind=kind, bbox=list(box), detail_id=detail)


def r(predicate='inserted_into', basis='drawing_reading', box=(.3, .2, .45, .3), source='iso', target='tr'):
    return dict(source=source, target=target, predicate=predicate, basis=basis, condition=None, bbox=list(box))


@pytest.mark.parametrize('rel,source,target,status,reason', [
    (r(), n('isolator'), n('transom'), 'proposed', None),
    (r('fits_into'), n('glazing_spacer'), n('transom'), 'proposed', None),
    (r('seals against'), n('gasket'), n('glass'), 'proposed', None),
    (r('followed_by', source='p1', target='p2'), n('position'), n('position'), 'invalid', 'source_is_position'),
    (r('fixed_to'), n('dimension'), n('transom'), 'invalid', 'source_is_dimension'),
    (r('fixed_to', target='iso'), n('isolator'), n('isolator'), 'invalid', 'self_reference'),
    (r('near'), n('gasket'), n('transom'), 'unresolved', 'unrecognized_predicate'),
    (r(basis='inference'), n('isolator'), n('transom'), 'unresolved', 'inferred_not_observed'),
    (r(box=(.3, .7, .4, .8)), n('isolator'), n('transom'), 'unresolved', 'evidence_outside_region'),
    (r(box=(0, 0, 1, 1)), n('isolator'), n('transom'), 'unresolved', 'no_evidence_locator'),
    (r(), n('isolator'), n('transom', box=(.3, .7, .4, .8)), 'unresolved', 'target_outside_region'),
    (r(), n('isolator', detail='mullion'), n('transom'), 'unresolved', 'endpoints_in_different_details'),
    (r(), n('unresolved_reference'), n('transom'), 'unresolved', 'source_unresolved_reference'),
])
def test_relationship_eligibility(rel, source, target, status, reason, alupco):
    actual, reasons = relationship_status(rel, source, target, False, alupco, TOP)
    assert actual == status
    assert (reason in reasons) if reason else reasons == []


def test_model_error_disputes_an_otherwise_valid_relation(alupco):
    assert relationship_status(r(), n('isolator'), n('transom'), True, alupco, TOP)[0] == 'disputed'


# --- End to end through the real export -----------------------------------------------

class Page15(FakeBackend):
    def execute(self, task, prompt, schema, folder, heartbeat=lambda: None):
        reply = super().execute(task, prompt, schema, folder, heartbeat)
        if task == 'identity':
            reply['output'] = dict(ALUPCO)
        elif task == 'extraction':
            unit = read(folder / 'scope.json')
            y0, y1 = unit['bbox'][1], unit['bbox'][3]
            box = lambda a, b: [.2, y0 + (y1 - y0) * a, .8, y0 + (y1 - y0) * b]
            far = [.2, .85, .8, .9] if y0 < .5 else [.2, .1, .8, .15]
            node = lambda i, kind, article, label, d='transom': dict(id=i, detail_id=d, article_id=article, kind=kind,
                label=label, material=None, quantity=None, callout=None, bbox=box(.3, .5))
            rel = lambda s, p, t, basis='drawing_reading', b=None: dict(source=s, predicate=p, target=t, basis=basis,
                condition=None, bbox=b or box(.3, .5))
            rows = [['Art.-Nr. 235 918-11.5 (Isolator 11.4mm)', '11.4'], ['Art.-Nr. 235 918-20.5 (Isolator 20.5mm)', '20.5'],
                    ['Art.-Nr. 235 545', '5 - 7 mm'], ['Art.-Nr. 237 351', '11 - 13 mm']]
            reply['output']['extraction'] = dict(
                pdf_page=unit['pdf_page'], page_kind='detail',
                details=[dict(id='transom', title='Transom section', drawing_refs=[], bbox=box(.1, .9)),
                         dict(id='steps', title='Installation positions', drawing_refs=[], bbox=box(.1, .9))],
                nodes=[node('iso', 'isolator', '235918', 'Isolator 235918 (20.5mm)'),
                       node('iso2', 'isolator', '235 918', 'Isolator'),
                       node('gk', 'gasket', '235456', 'Glazing gasket'),
                       node('sp', 'glazing_spacer', '32476', 'Glazing spacer'),
                       node('tr', 'transom', None, 'Transom'),
                       node('dim', 'dimension', '20.5', 'Isolator size'),
                       node('p1', 'position', None, 'Position 1', 'steps'),
                       node('p2', 'position', None, 'Position 2', 'steps')],
                relations=[rel('iso', 'inserted_into', 'tr'), rel('sp', 'fits_into', 'tr'),
                           rel('gk', 'near', 'tr'), rel('p1', 'followed_by', 'p2'),
                           rel('gk', 'seals_against', 'tr', b=far), rel('iso', 'fixed_to', 'gk', basis='inference'),
                           rel('sp', 'supports', 'p1')],
                measurements=[], instructions=[], unresolved=[],
                tables=[dict(id='t', title='Rubber gaskets & isolators', columns=['Art.-Nr.', 'Size'], units=[None, None],
                             rows=[dict(cells=c, bbox=box(.6, .7)) for c in rows], footnotes=[], bbox=box(.55, .8))])
        return reply


def test_page15_export(tmp_path):
    pdf = pdf_file(tmp_path / 'fixture.pdf')
    run = Path(FacadeExtractionAgent(out=tmp_path / 'runs', budget_usd=10,
                                     backend_factory=lambda run, budget: Page15([]))(pdf_url=pdf.as_uri()).output_directory)
    data = run / 'data'
    components = {c['article_id']: c for c in read(data / 'components.json')}
    assert set(components) == {'235918', '235456', '32476', '235918-11.5', '235918-20.5', '235545', '237351'}
    literals = {o['literal_article_id'] for o in components['235918']['observations']}
    assert literals == {'235918', '235 918'}  # both printed spellings retained, one component
    assert read(run / 'document.json')['manufacturer_profile'] == 'alupco'
    status = {(x['source'], x['predicate'], x['target']): x for x in read(data / 'relationships.json')}
    expected = {('iso', 'inserted_into', 'tr'): 'proposed', ('sp', 'fits_into', 'tr'): 'proposed',
                ('gk', 'near', 'tr'): 'unresolved', ('p1', 'followed_by', 'p2'): 'invalid',
                ('gk', 'seals_against', 'tr'): 'unresolved', ('iso', 'fixed_to', 'gk'): 'unresolved',
                ('sp', 'supports', 'p1'): 'invalid'}
    assert {k: v['relationship_status'] for k, v in status.items()} == expected
    assert status[('sp', 'fits_into', 'tr')]['canonical_predicate'] == 'inserted_into'
    for row in status.values():
        assert row['graph_eligible'] == (row['relationship_status'] == 'proposed')
        assert row['human_confirmed'] is False
    questions = read(data / 'questions.json')
    assert not [q for q in questions if q['category'] == 'unparsed_table_article_cell']
    assert any(q['category'] == 'unresolved_article_label' and q['message'] == '20.5' for q in questions)
