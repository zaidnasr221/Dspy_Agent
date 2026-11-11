from pathlib import Path

import pytest

from facade_dspy.catalogue_databases import _edge_distance
from facade_dspy.program import FacadeExtractionAgent
from facade_dspy.storage import read
from test_agent import FakeBackend, pdf_file


@pytest.mark.parametrize('value, offset, offset_unit, expected', [
    (20, 3, 'mm', 23),
    ([20, 30], -3, 'mm', [17, 27]),
    (20, 3, 'cm', None),
    (20, -21, 'mm', None),
])
def test_edge_spacing_applies_signed_offset_only_with_compatible_units(value, offset, offset_unit, expected):
    record = dict(value=value, unit='mm', edge_reference=dict(
        rail_end='x1', measured_from='dimension_line', offset=offset, offset_unit=offset_unit,
    ))
    result = _edge_distance(record)
    assert result['value'] == expected
    assert result['source_value'] == value


class CatalogueBackend(FakeBackend):
    def execute(self, task, prompt, schema, folder, heartbeat=lambda: None):
        reply = super().execute(task, prompt, schema, folder, heartbeat)
        if task == 'identity':
            reply['output']['evidence_pages'] = [1]
        if task != 'extraction':
            return reply
        page = reply['output']['extraction']
        detail = page['details'][0]
        detail.update(system_type='CC', position='Mullion', view_type='assembly_orthographic')
        article = page['nodes'][0]
        article.update(kind='pressure_plate', article_type='bar', color='Black', quantity=2)
        for prop, value, reference in (
            ('depth', 52, None),
            ('edge_spacing', 20, dict(rail_end='x0', measured_from='dimension_line', offset=-3, offset_unit='mm')),
            ('edge_spacing', 20, dict(rail_end='x1', measured_from='dimension_line', offset=None, offset_unit=None)),
            ('spacing', 100, None),
        ):
            page['measurements'].append(dict(
                subject='a', property=prop, value=value, unit='mm', datum=None, condition=None,
                bbox=article['bbox'], evidence='Fixture dimension', edge_reference=reference,
            ))
        table = page['tables'][0]
        table.update(columns=['Article', 'Finish', 'Notes'], units=[None, None, None])
        table['rows'][0]['cells'] = ['A100', 'Anodised', 'Fixture note']
        return reply


def test_article_and_assembly_databases_publish_and_replay(tmp_path):
    pdf = pdf_file(tmp_path / 'fixture.pdf', pages=1)
    calls = []
    run = FacadeExtractionAgent(
        out=tmp_path / 'live', budget_usd=1, backend_factory=lambda *_: CatalogueBackend(calls),
    )(pdf_url=pdf.as_uri())
    snapshot = Path(run.snapshot_directory)
    articles = read(snapshot / 'data/article_database.json')
    plate = next(row for row in articles if row['profile_id'] == 'A100')
    assert plate['profile_role'] == 'pressure_plate'
    assert plate['finish'] == 'Anodised'
    assert plate['color'] == 'Black'
    assert plate['occurrences'][0]['article_type'] == 'bar'
    assert plate['occurrences'][0]['context']['system_type'] == 'CC'
    assert plate['depth'][0]['value'] == 52
    assert plate['spacing'][0]['value'] == 100
    assert [(row['value'], row['edge_reference']['rail_end']) for row in plate['edge_spacing']] == [(17, 'x0'), (None, 'x1')]
    assert plate['table_observations'][0]['fields'][-1]['value'] == 'Fixture note'
    assert plate['quantity'][0]['value'] == 2
    relationships = read(snapshot / 'data/assembly_database.json')
    fixing = next(row for row in relationships if row['relation'] == 'fixed')
    assert fixing['fastener_assigned']['profile_id'] == 'V100'
    assert fixing['fastener_assigned']['type'] == 'fastener'

    def forbidden(*_):
        raise AssertionError('Replay attempted to instantiate a backend')

    replay = FacadeExtractionAgent(
        out=tmp_path / 'replay', reuse=snapshot, replay=True, backend_factory=forbidden,
    )(pdf_url=pdf.as_uri())
    replay_data = Path(replay.snapshot_directory) / 'data'
    assert read(replay_data / 'article_database.json') == articles
    assert read(replay_data / 'assembly_database.json') == relationships
