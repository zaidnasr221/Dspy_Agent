from pathlib import Path

import pytest

from facade_dspy.program import FacadeExtractionAgent
from facade_dspy.storage import dump, read
from test_agent import FakeBackend, pdf_file


def test_replay_preserves_disputed_committed_region_and_review(tmp_path):
    pdf = pdf_file(tmp_path / 'fixture.pdf', pages=2)
    result = FacadeExtractionAgent(out=tmp_path/'live', budget_usd=1,
        backend_factory=lambda r,b: FakeBackend([], 'review_error'))(pdf_url=pdf.as_uri())
    original = Path(result.snapshot_directory)
    assert read(original/'data/relationships.json')[0]['relationship_status']=='disputed'
    replay = FacadeExtractionAgent(out=tmp_path/'replay', reuse=result.output_directory,
                                   replay=True)(pdf_url=pdf.as_uri())
    snapshot = Path(replay.snapshot_directory)
    assert replay.coverage['completed_region_proposals']==1
    assert replay.coverage['complete_proposal_pages']==[]
    source_reviews=list((original/'reviews').glob('*.json'))
    assert source_reviews
    for review in source_reviews:
        assert read(snapshot/'reviews'/review.name)==read(review)
    assert read(snapshot/'data/relationships.json')[0]['relationship_status']=='disputed'
    assert all(not row['graph_eligible'] for row in read(snapshot/'data/relationships.json'))
    assert read(snapshot/'data/graph_relationships.json')==[]
    dump(source_reviews[0],dict(read(source_reviews[0]),findings=[]))
    with pytest.raises(ValueError,match='Snapshot replay integrity'):
        FacadeExtractionAgent(out=tmp_path/'replay_tampered', reuse=result.output_directory,
                              replay=True)(pdf_url=pdf.as_uri())
    (original/'replay_manifest.json').unlink()
    with pytest.raises(ValueError,match='manifest is missing'):
        FacadeExtractionAgent(out=tmp_path/'replay_missing_manifest', reuse=result.output_directory,
                              replay=True)(pdf_url=pdf.as_uri())


def test_run_keeps_pinned_profile_on_resume_and_replay(tmp_path,monkeypatch):
    pdf=pdf_file(tmp_path/'fixture.pdf')
    calls=[]
    def run():
        return FacadeExtractionAgent(out=tmp_path/'live',budget_usd=1,
            backend_factory=lambda r,b: FakeBackend(calls))(pdf_url=pdf.as_uri())
    original=run()
    run_path=Path(original.output_directory)
    pin=read(run_path/'manufacturer_profile.json')
    assert pin['version']==pin['profile_version']==1 and len(pin['scope_sha256'])==64
    assert len(pin['profile_sha256'])==64
    extra=tmp_path/'profiles'
    dump(extra/'demo.json',{'id':'demo','article_patterns':[r'ZZ\d+']})
    monkeypatch.setenv('FACADE_MANUFACTURER_DIR',str(extra))
    before=list(calls)
    resumed=run()
    assert calls==before
    assert read(Path(resumed.snapshot_directory)/'manufacturer_profile.json')==pin
    replay=FacadeExtractionAgent(out=tmp_path/'replay',reuse=resumed.output_directory,
                                  replay=True)(pdf_url=pdf.as_uri())
    assert read(Path(replay.snapshot_directory)/'manufacturer_profile.json')==pin
    assert read(Path(replay.snapshot_directory)/'data/components.json')==read(Path(resumed.snapshot_directory)/'data/components.json')
    dump(run_path/'manufacturer_profile.json',dict(pin,profile_sha256='0'*64))
    with pytest.raises(ValueError,match='Manufacturer profile'):
        run()


def test_replay_legacy_proposals_without_reviews_are_ineligible(tmp_path):
    from facade_dspy.storage import filehash
    from test_agent import page_result

    pdf=pdf_file(tmp_path/'fixture.pdf',pages=1)
    legacy=tmp_path/'legacy'
    dump(legacy/'document.json',{'sha256':filehash(pdf),'document_id':'legacy'})
    dump(legacy/'full/pages/p0001.json',page_result(1))
    replay=FacadeExtractionAgent(out=tmp_path/'replay',reuse=legacy,replay=True)(pdf_url=pdf.as_uri())
    relations=read(Path(replay.snapshot_directory)/'data/relationships.json')
    assert relations and all(not row['graph_eligible'] for row in relations)
    assert 'legacy_review_unavailable' in relations[0]['status_reasons']


def test_checkpoint_audit_can_refuse_changed_inputs_without_losing_commit(tmp_path):
    from facade_dspy.queue import WorkQueue

    source=tmp_path/'input.json'
    output=tmp_path/'output.json'
    dump(source,{'original':True})
    dump(output,{'completed':True})
    queue=WorkQueue(tmp_path/'work')
    try:
        queue.register('test','sample','sample',[source])
        queue.run('test',lambda spec:{'outputs':[output]})
        commit=queue.db.execute('SELECT id,checkpoint FROM attempts').fetchone()
        receipt=queue.path(commit['checkpoint']).read_bytes()
        dump(source,{'changed':True})
        assert queue.audit(preserve=True)==['test']
        assert queue.db.execute('SELECT status FROM jobs').fetchone()[0]=='completed'
        assert queue.path(commit['checkpoint']).read_bytes()==receipt
    finally:
        queue.db.close()


def test_alupco_system_and_numeric_first_ids():
    from facade_dspy import manufacturers
    compact=manufacturers.resolve({'manufacturer_brand':'ALUPCO','system_id':'50SG'})
    spaced=manufacturers.resolve({'manufacturer_brand':'ALUPCO','system_id':'50 SG'})
    assert compact['system_key']==spaced['system_key']
    profile=compact['profile']
    assert manufacturers.literal_article('50SG02','gasket',profile)
    assert manufacturers.table_articles('50SG01 / 50SG02',profile)==(['50SG01','50SG02'],[],'')
    assert not manufacturers.literal_article('Transom','profile',profile)
    assert not manufacturers.literal_article('P001','position',profile)


def test_full_page_placeholder_evidence_is_ineligible():
    from facade_dspy.exports import relationship_status
    edge=dict(source='a',target='b',predicate='fixed_to',basis='drawing_reading',bbox=[0,0,1,1])
    endpoint=dict(kind='profile',detail_id='detail',bbox=[.1,.1,.2,.2])
    status,reasons=relationship_status(edge,endpoint,endpoint,False,scope=[0,0,1,1])
    assert status=='unresolved' and 'no_evidence_locator' in reasons


def test_benchmark_respects_manufacturer_drawing_patterns():
    from facade_dspy.benchmark import evaluate
    from test_benchmark import inputs

    truth,prediction=inputs()
    truth['cases'][0]['expected']['nodes'][0]['article_id']='K1013900'
    prediction['extraction']['nodes'][0]['article_id']='K1013900'
    with pytest.raises(ValueError,match='Drawing ID'):
        evaluate(truth,[prediction])
    report=evaluate(truth,[prediction],profiles_by_source={'a'*64:{'drawing_reference_patterns':[r'DRG-\d+']}})
    assert report['metrics']['relationships']['f1']==1
