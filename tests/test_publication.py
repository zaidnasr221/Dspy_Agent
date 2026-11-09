"""Offline graph and publication regressions using synthetic PDF evidence."""
from pathlib import Path
import zipfile
import pytest

from facade_dspy.cli import zip_results
from facade_dspy.program import FacadeExtractionAgent, result_path
from facade_dspy.storage import dump, read
from test_agent import FakeBackend, page_result, pdf_file


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
    assert len(entities) == 4 # two whole-page regions x two nodes
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


def test_publication_excludes_uncommitted_proposals(tmp_path):
    pdf = pdf_file(tmp_path / 'fixture.pdf', pages=1)
    agent = FacadeExtractionAgent(out=tmp_path / 'runs', index_only=True)
    first = agent(pdf_url=pdf.as_uri())
    run = Path(first.output_directory)
    unit = read(run / 'units.json')[0]
    orphan = result_path(run, unit)
    dump(orphan, dict(unit=unit, extraction=page_result(1),
        metadata=dict(source_sha256=read(run / 'source.json')['source_sha256'])))

    result = agent(pdf_url=pdf.as_uri())
    assert result.coverage['completed_region_proposals'] == 0
    snapshot = Path(result.snapshot_directory)
    assert read(snapshot / 'data/entities.json') == []
    assert orphan.exists()
    with zipfile.ZipFile(zip_results(run, tmp_path / 'published.zip')) as archive:
        assert 'review.html' in archive.namelist()
        assert not any(name.startswith('proposals/') for name in archive.namelist())


def test_live_publication_ignores_identity_without_a_commit(tmp_path):
    pdf = pdf_file(tmp_path / 'fixture.pdf')
    out = tmp_path / 'runs'
    indexed = FacadeExtractionAgent(out=out, index_only=True)(pdf_url=pdf.as_uri())
    run = Path(indexed.output_directory)
    identity = run / 'identity.json'
    dump(identity, dict(read(identity), manufacturer_brand='Uncommitted brand'))
    calls = []
    agent = FacadeExtractionAgent(out=out, budget_usd=10,
                                  backend_factory=lambda run, budget: FakeBackend(calls))
    result = agent(pdf_url=pdf.as_uri())
    assert calls.count('identity') == 1
    assert read(Path(result.snapshot_directory) / 'document.json')['manufacturer_brand'] == 'Demo'
    assert read(Path(indexed.snapshot_directory) / 'document.json')['manufacturer_brand'] is None
    assert read(run / 'work' / 'progress.json')['by_stage']['document_identity'] == {'completed': 1}


def test_failed_export_preserves_last_snapshot(tmp_path, monkeypatch):
    from facade_dspy import exports
    pdf = pdf_file(tmp_path / 'fixture.pdf', pages=1)
    agent = FacadeExtractionAgent(out=tmp_path / 'runs', index_only=True)
    first = agent(pdf_url=pdf.as_uri())
    run = Path(first.output_directory)
    pointer = (run / 'publication.json').read_bytes()
    previous = Path(first.snapshot_directory)
    before = (previous / 'review.html').read_bytes()

    def fail(*args, **kwargs):
        raise RuntimeError('synthetic export interruption')

    monkeypatch.setattr(exports, 'dataset', fail)
    with pytest.raises(RuntimeError, match='synthetic export interruption'):
        agent(pdf_url=pdf.as_uri())
    assert (run / 'publication.json').read_bytes() == pointer
    assert (previous / 'review.html').read_bytes() == before
