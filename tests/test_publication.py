"""Offline graph and publication regressions using synthetic PDF evidence."""
from pathlib import Path

from facade_dspy.program import FacadeExtractionAgent
from facade_dspy.storage import read
from test_agent import FakeBackend, pdf_file


class GraphFixture(FakeBackend):
    def execute(self, task, prompt, schema, folder, heartbeat=lambda: None):
        reply = super().execute(task, prompt, schema, folder, heartbeat)
        if task == 'extraction':
            result = reply['output']['extraction']
            result['instructions'].append(dict(detail_id='d', sequence=1, text='Fit connector',
                condition=None, article_refs=['A100'], bbox=result['details'][0]['bbox']))
        return reply


def test_graph_keys_connect_across_regions_and_keep_source_ids(tmp_path):
    pdf = pdf_file(tmp_path / 'fixture.pdf')
    agent = FacadeExtractionAgent(out=tmp_path / 'runs', budget_usd=10,
                                  backend_factory=lambda run, budget: GraphFixture([]))
    run = Path(agent(pdf_url=pdf.as_uri()).output_directory)
    data = run / 'data'
    entities = {row['canonical_id']: row for row in read(data / 'entities.json')}
    details = {row['canonical_id']: row for row in read(data / 'details.json')}
    components = {row['canonical_id'] for row in read(data / 'components.json')}
    assert len(entities) == 8
    assert entities.keys().isdisjoint(details)
    for entity in entities.values():
        assert details[entity['canonical_detail_id']]['id'] == entity['detail_id']
        assert entity['canonical_component_id'] in components
    for row in read(data / 'relationships.json'):
        assert entities[row['canonical_source_id']]['id'] == row['source']
        assert entities[row['canonical_target_id']]['id'] == row['target']
    for row in read(data / 'measurements.json'):
        assert entities[row['canonical_subject_id']]['id'] == row['subject']
        assert row['provenance']['region_id'] == entities[row['canonical_subject_id']]['provenance']['region_id']
    for row in read(data / 'instructions.json'):
        assert details[row['canonical_detail_id']]['id'] == row['detail_id']
    tables = {row['canonical_id']: row for row in read(data / 'tables.json')}
    for row in read(data / 'table_rows.json'):
        assert tables[row['canonical_table_id']]['id'] == row['original_table_id']
