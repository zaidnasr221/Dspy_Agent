from pathlib import Path

import pytest

from facade_dspy.program import FacadeExtractionAgent
from facade_dspy.storage import dump, read
from test_agent import FakeBackend, pdf_file


class NamedReviewError(FakeBackend):
    """The review names one relationship as wrong; the verifier's verdict comes from 'mode'."""
    def execute(self,task,prompt,schema,folder,heartbeat=lambda:None):
        reply=super().execute(task,prompt,schema,folder,heartbeat)
        if task=='review':
            unit=read(folder/'scope.json')
            reply['output']['findings']=[dict(severity='error',bbox=unit['bbox'],
                message='Relationship r1: A100 fastened_by V100 is not shown on the page.')]
        return reply


def test_replay_preserves_disputed_committed_region_and_review(tmp_path):
    pdf = pdf_file(tmp_path / 'fixture.pdf', pages=2)
    result = FacadeExtractionAgent(out=tmp_path/'live', budget_usd=1,
        backend_factory=lambda r,b: NamedReviewError([]))(pdf_url=pdf.as_uri())
    # The verifier supported the relationship the review rejects: a real conflict, so the pilot still stops.
    assert result.coverage['status']=='pilot_source_check_found_errors'
    original = Path(result.snapshot_directory)
    assert read(original/'data/relationships.json')[0]['relationship_status']=='disputed'
    replay = FacadeExtractionAgent(out=tmp_path/'replay', reuse=result.output_directory,
                                   replay=True)(pdf_url=pdf.as_uri())
    snapshot = Path(replay.snapshot_directory)
    assert replay.coverage['completed_region_proposals']==1
    assert replay.coverage['complete_proposal_pages']==[1] # the pilot page is one whole-page region
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


def test_resume_and_replay_keep_results_and_relationship_checks(tmp_path):
    pdf=pdf_file(tmp_path/'fixture.pdf')
    calls=[]
    def run():
        return FacadeExtractionAgent(out=tmp_path/'live',budget_usd=1,
            backend_factory=lambda r,b: FakeBackend(calls))(pdf_url=pdf.as_uri())
    original=run()
    assert not (Path(original.output_directory)/'manufacturer_profile.json').exists()
    before=list(calls)
    resumed=run()
    assert calls==before # no model call on resume
    snapshot=Path(resumed.snapshot_directory)
    assert list((snapshot/'verifications').glob('*.json'))
    replay=FacadeExtractionAgent(out=tmp_path/'replay',reuse=resumed.output_directory,
                                  replay=True)(pdf_url=pdf.as_uri())
    replayed=Path(replay.snapshot_directory)
    assert calls==before
    for name in ('components','relationships','graph_relationships'):
        assert read(replayed/f'data/{name}.json')==read(snapshot/f'data/{name}.json')
    assert all(r['graph_eligible'] and r['verification']=='supported' for r in read(replayed/'data/relationships.json'))


def test_checkpoints_from_the_profile_era_resume_without_new_model_calls(tmp_path):
    """Region jobs committed while manufacturer profiles existed listed manufacturers.py and the pin file."""
    import json
    from facade_dspy.queue import WorkQueue
    from facade_dspy.storage import objecthash
    pdf=pdf_file(tmp_path/'fixture.pdf')
    calls=[]
    def run():
        return FacadeExtractionAgent(out=tmp_path/'live',budget_usd=1,
            backend_factory=lambda r,b: FakeBackend(calls))(pdf_url=pdf.as_uri())
    run_path=Path(run().output_directory)
    dump(run_path/'manufacturer_profile.json',{'version':1,'retired':True})
    queue=WorkQueue(run_path/'work')
    try:
        for row in queue.db.execute("SELECT id,spec FROM jobs WHERE stage='region_extraction'").fetchall():
            spec=json.loads(row['spec'])
            spec['inputs'].pop('code:rules.py')
            spec['inputs'].update({'code:manufacturers.py':'0'*64,'run:manufacturer_profile.json':'1'*64})
            encoded=json.dumps(spec,sort_keys=True)
            import hashlib
            queue.db.execute('UPDATE jobs SET spec=?,spec_hash=? WHERE id=?',(encoded,hashlib.sha256(encoded.encode()).hexdigest(),row['id']))
            checkpoint=queue.db.execute('SELECT checkpoint FROM attempts WHERE job_id=? ORDER BY ordinal DESC LIMIT 1',(row['id'],)).fetchone()[0]
            receipt=read(queue.path(checkpoint));receipt['spec']=spec;dump(queue.path(checkpoint),receipt)
        # Keep dependents' receipts consistent with the rewritten checkpoints, as in a real historical run.
        from facade_dspy.queue import digest
        for row in queue.db.execute("SELECT job_id,checkpoint FROM attempts WHERE status='completed'").fetchall():
            receipt=read(queue.path(row['checkpoint']))
            for dep in receipt.get('dependency_checkpoint_hashes',{}):
                latest=queue.db.execute("SELECT checkpoint FROM attempts WHERE job_id=? AND status='completed' ORDER BY ordinal DESC LIMIT 1",(dep,)).fetchone()[0]
                receipt['dependency_checkpoint_hashes'][dep]=digest(queue.path(latest))
            dump(queue.path(row['checkpoint']),receipt)
        queue.db.commit()
        assert queue.audit(preserve=True)==[]
    finally:
        queue.db.close()
    before=list(calls)
    resumed=run()
    assert calls==before
    assert resumed.coverage['complete_proposal_pages']==[1,2]


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
    from facade_dspy import rules
    assert rules.identity_keys({'manufacturer_brand':'ALUPCO','system_id':'50SG'})==rules.identity_keys(
        {'manufacturer_brand':'Alupco LLC','system_id':'50 SG'})==('alupco','50sg')
    assert rules.literal_article('50SG02','gasket')
    assert rules.table_articles('50SG01 / 50SG02')==(['50SG01','50SG02'],[],'')
    assert not rules.literal_article('Transom','profile')
    assert not rules.literal_article('P001','position')


def test_full_page_placeholder_evidence_is_ineligible():
    from facade_dspy.exports import relationship_status
    edge=dict(source='a',target='b',predicate='fixed_to',basis='drawing_reading',bbox=[0,0,1,1])
    endpoint=dict(kind='profile',detail_id='detail',bbox=[.1,.1,.2,.2])
    status,reasons=relationship_status(edge,endpoint,endpoint,False,scope=[0,0,1,1])
    assert status=='unresolved' and 'no_evidence_locator' in reasons


def test_benchmark_rejects_drawing_references_as_articles():
    from facade_dspy.benchmark import evaluate
    from test_benchmark import inputs

    truth,prediction=inputs()
    truth['cases'][0]['expected']['nodes'][0]['article_id']='K1013900'
    prediction['extraction']['nodes'][0]['article_id']='K1013900'
    with pytest.raises(ValueError,match='Drawing ID'):
        evaluate(truth,[prediction])


def test_review_dispute_is_scoped_and_settled_errors_do_not_stop(tmp_path):
    """A review error about a relationship the verifier already rejected excludes only that relationship."""
    pdf=pdf_file(tmp_path/'fixture.pdf',pages=2)
    calls=[]
    result=FacadeExtractionAgent(out=tmp_path/'live',budget_usd=1,
        backend_factory=lambda r,b: NamedReviewError(calls,'relation_unsupported'))(pdf_url=pdf.as_uri())
    assert result.coverage['status']=='processing_finished_with_coverage_report'
    assert result.coverage['complete_proposal_pages']==[1,2]
    assert calls.count('extraction')==2
    relations=read(Path(result.snapshot_directory)/'data/relationships.json')
    assert all(r['relationship_status']=='invalid' and r['verification']=='unsupported' for r in relations)


def test_unattributed_review_error_disputes_no_relationship_but_still_stops(tmp_path):
    pdf=pdf_file(tmp_path/'fixture.pdf',pages=2)
    result=FacadeExtractionAgent(out=tmp_path/'live',budget_usd=1,
        backend_factory=lambda r,b: FakeBackend([],'review_error'))(pdf_url=pdf.as_uri())
    assert result.coverage['status']=='pilot_source_check_found_errors' # critical: names no relationship
    relations=read(Path(result.snapshot_directory)/'data/relationships.json')
    assert relations and all(r['relationship_status']=='proposed' and r['graph_eligible'] for r in relations)
    questions=read(Path(result.snapshot_directory)/'data/questions.json')
    assert any(q['category']=='model_source_check' for q in questions)


def test_finding_claims_need_both_endpoints():
    from facade_dspy.exports import finding_claims
    nodes={'l50SG03':dict(id='l50SG03',article_id='50SG03'),'l50SG01':dict(id='l50SG01',article_id='50SG01'),
           'lframe':dict(id='lframe',article_id=None)}
    box=[.1,.1,.2,.2]
    rels=[dict(source='l50SG03',predicate='seals_against',target='l50SG01',bbox=box),
          dict(source='l50SG03',predicate='inserted_into',target='lframe',bbox=box)]
    finding=lambda m:dict(severity='error',message=m,bbox=box)
    assert finding_claims(finding('The relationship l50SG03 seals_against l50SG01 is unsupported.'),rels,nodes)=={'r1'}
    assert finding_claims(finding('Spacer gasket 50SG03 does not touch sealing gasket 50SG01.'),rels,nodes)=={'r1'}
    assert finding_claims(finding('Gasket 50SG03 article number looks misread.'),rels,nodes)==set()


def test_only_a_relationship_finding_can_clear_a_pilot_error():
    from facade_dspy.exports import finding_claims, unresolved_errors

    nodes={'a':dict(id='a',article_id='235 918'),'b':dict(id='b',article_id='V100')}
    box=[.1,.1,.2,.2]
    rels=[dict(source='a',predicate='fastened_by',target='b',bbox=box)]
    mismatch=dict(severity='error',bbox=box,message='Article IDs 235918 and V100 are misread.')
    assert unresolved_errors([mismatch],rels,nodes,{'r1':'unsupported'})==[mismatch]
    variant=dict(mismatch,message='The relationship 235918-20.5 fastened_by V100 is unsupported.')
    assert finding_claims(variant,rels,nodes)==set()
    other_detail=dict(mismatch,bbox=[.7,.7,.8,.8],
                      message='The relationship 235918 fastened_by V100 is unsupported.')
    assert finding_claims(other_detail,rels,nodes)==set()
    exact=dict(mismatch,message='Relationship r1: The two parts do not touch.')
    assert finding_claims(exact,rels,nodes)=={'r1'}
    assert unresolved_errors([exact],rels,nodes,{'r1':'unsupported'})==[]


def test_pilot_uses_only_committed_relationship_verdicts(tmp_path):
    from facade_dspy.storage import objecthash
    from facade_dspy.tool_protocol import IncompleteRead

    class OrphanVerdict(NamedReviewError):
        def execute(self,task,prompt,schema,folder,heartbeat=lambda:None):
            if task=='relation_check':
                raise IncompleteRead('Fixture failure before commit')
            reply=super().execute(task,prompt,schema,folder,heartbeat)
            if task=='review':
                uid=reply['output']['unit_id']
                target=folder.parents[1]/'verifications'/(objecthash(uid)[:24]+'.json')
                dump(target,dict(unit_id=uid,verdicts=[dict(
                    claim_id='r1',source='a',predicate='fastened_by',target='s',verdict='unsupported')]))
            return reply

    pdf=pdf_file(tmp_path/'fixture.pdf')
    result=FacadeExtractionAgent(out=tmp_path/'live',budget_usd=1,
        backend_factory=lambda r,b:OrphanVerdict([]))(pdf_url=pdf.as_uri())
    assert result.coverage['status']=='pilot_source_check_found_errors'


@pytest.mark.parametrize('rejected_copy',[False,True])
def test_duplicate_graph_edges_keep_candidate_and_distinct_conditions(tmp_path,rejected_copy):
    class Duplicate(FakeBackend):
        def execute(self,task,prompt,schema,folder,heartbeat=lambda:None):
            reply=super().execute(task,prompt,schema,folder,heartbeat)
            if task=='extraction':
                rels=reply['output']['extraction']['relations']
                rels.extend([dict(rels[0],predicate='fastened_with'),
                             dict(rels[0],condition='Alternate assembly')])
            elif task=='relation_check' and rejected_copy:
                reply['output']['verdicts'][1]['verdict']='unsupported'
            return reply

    pdf=pdf_file(tmp_path/'fixture.pdf')
    result=FacadeExtractionAgent(out=tmp_path/'live',budget_usd=1,
        backend_factory=lambda r,b:Duplicate([]))(pdf_url=pdf.as_uri())
    data=Path(result.snapshot_directory)/'data'
    rels=read(data/'relationships.json')
    assert len(rels)==6
    graph=read(data/'graph_relationships.json')
    if rejected_copy:
        assert len(graph)==2 and all(r['condition']=='Alternate assembly' for r in graph)
        assert sum('conflicting_duplicate_relationship' in r['status_reasons'] for r in rels)==2
    else:
        assert len(graph)==4
        assert sum('duplicate_relationship' in r['status_reasons'] for r in rels)==2


def test_non_component_kind_uses_normalized_spelling():
    from facade_dspy.exports import relationship_status

    edge=dict(source='a',target='b',predicate='fixed_to',basis='drawing_reading',evidence='leader')
    status,reasons=relationship_status(edge,dict(kind='profile'),dict(kind=' Drawing reference '),False)
    assert status=='invalid' and 'target_is_drawing_reference' in reasons
    status,reasons=relationship_status(dict(edge,predicate='fastened_with'),dict(kind='profile'),dict(kind='glass'),False)
    assert status=='invalid' and 'fastened_by_target_is_not_fastener' in reasons
