"""MOCK pipeline run on the real 5-page ALUPCO subset (pages 1, 12-15). Fake model, no network.

Run from the project folder: FACADE_MOCK_OUT=<new folder> python -B evaluation/mock_alupco_pipeline.py
"""
import os, sys, socket
from pathlib import Path
from copy import deepcopy

def forbidden(*args, **kwargs):
    raise AssertionError('MOCK run forbids network connections')
socket.socket.connect=forbidden
socket.socket.connect_ex=forbidden
os.environ['PYTHON_DOTENV_DISABLED']='1'
os.environ['LITELLM_LOCAL_MODEL_COST_MAP']='True'
for name in ('OPENAI_API_KEY','ANTHROPIC_API_KEY'):
    os.environ.pop(name,None)
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path('tests').resolve()))
from test_agent import FakeBackend, page_result
from facade_dspy.benchmark import evaluate
from facade_dspy.cli import zip_results
from facade_dspy.program import FacadeExtractionAgent
from facade_dspy.storage import read,dump,filehash
from facade_dspy.schema import validate
import polars as pl
import zipfile,hashlib

original=Path('test_results/alupco_5pages_f5f8e9170c664c6ba0d2ce0bf77d926e') # optional saved page subset
pdf=(original/'selected_pages.pdf').resolve()
out=Path(os.environ['FACADE_MOCK_OUT']).resolve()
out.mkdir(parents=True,exist_ok=False)
print(out,flush=True)
if pdf.is_file():
    dump(out/'page_selection.json',read(original/'page_selection.json'))
else:
    import pymupdf
    source=Path('data/50SG ALUPCO PDF COMBINED.pdf').resolve()
    pdf=out/'selected_pages.pdf'
    pages=[1,12,13,14,15]
    with pymupdf.open(source) as full, pymupdf.open() as selected:
        for number in pages:
            selected.insert_pdf(full,from_page=number-1,to_page=number-1)
        selected.save(pdf)
    dump(out/'page_selection.json',dict(original_pdf=str(source),original_pdf_sha256=filehash(source),
        pages=[dict(test_pdf_page=i,original_pdf_page=n) for i,n in enumerate(pages,1)]))
from facade_dspy.tool_protocol import InsufficientCredits
calls=[]
interrupt=[True]
class MockBackend(FakeBackend):
    def execute(self,task,prompt,schema,folder,heartbeat=lambda:None):
        if task=='review' and interrupt[0]:
            interrupt[0]=False
            calls.append('simulated_billing_stop')
            raise InsufficientCredits('MOCK interruption during Source Review')
        receipt=super().execute(task,prompt,schema,folder,heartbeat)
        if task=='extraction':
            receipt['output']['extraction']['measurements'][0]['unit']='mm'
        return receipt

def run():
    return FacadeExtractionAgent(out=out/'outputs',budget_usd=1,backend='opencode',
        model='openai/mock-fixture',backend_factory=lambda run,budget:MockBackend(calls))(pdf_url=pdf.as_uri())
try:
    run()
except InsufficientCredits:
    interrupted=out/'outputs'/filehash(pdf)[:20]
    before_resume=read(interrupted/'work'/'attempts.json')
    committed_before=[a for a in before_resume if a['status']=='completed']
    # 5 index + identity + pilot extraction + its relationship check, committed before the stop.
    assert len(committed_before)==8, len(committed_before)
    assert 'relation_check' in calls
    assert not (interrupted/'publication.json').exists()
else:
    raise AssertionError('Expected one simulated billing stop')
first=run()
root=Path(first.output_directory)
snapshot=Path(first.snapshot_directory)
coverage=first.coverage
assert coverage['indexed_pages']==5 and coverage['complete_proposal_pages']==[1,2,3,4,5]
assert coverage['human_engineering_approved_records']==0 and read(snapshot/'pilot_validation.json')['passed']
assert read(snapshot/'cost_summary.json')['reported_cost_usd']==0
assert filehash(snapshot/'source.pdf')==filehash(pdf)
data=snapshot/'data'
datasets={}
for path in sorted(data.glob('*.json')):
    name=path.stem
    count=len(read(path))
    assert pl.read_parquet(path.with_suffix('.parquet')).height==count
    assert pl.read_csv(path.with_suffix('.csv')).height==count
    datasets[name]=count
entities={v['canonical_id']:v for v in read(data/'entities.json')}
details={v['canonical_id']:v for v in read(data/'details.json')}
tables={v['canonical_id']:v for v in read(data/'tables.json')}
components={v['canonical_id']:v for v in read(data/'components.json')}
assert len(entities)==datasets['entities'] and len(details)==datasets['details']
for e in entities.values():
    assert details[e['canonical_detail_id']]['id']==e['detail_id']
    if e['canonical_component_id']:
        assert components[e['canonical_component_id']]['article_id']==e['article_id']
# The fake reader claims 'V100', which is not printed on these real ALUPCO pages (and is not in its own
# transcription or tables): grounding keeps it out of the component registry and raises a question.
ungrounded={e['article_id'] for e in entities.values() if e['article_status']=='ungrounded_article'}
assert ungrounded=={'V100'}, ungrounded
assert {c['article_id'] for c in components.values()}=={'A100'}
assert any(q['category']=='ungrounded_article' and q['message']=='V100' for q in read(data/'questions.json'))
for v in read(data/'relationships.json'):
    for key in ('source','target'):
        e=entities[v['canonical_'+key+'_id']]
        assert e['id']==v[key] and e['provenance']['region_id']==v['provenance']['region_id']
for v in read(data/'measurements.json'):
    assert entities[v['canonical_subject_id']]['id']==v['subject']
for v in read(data/'table_rows.json'):
    assert tables[v['canonical_table_id']]['id']==v['original_table_id']
for n in range(1,6):
    merged=read(snapshot/'full'/'pages'/f'p{n:04}.json')
    assert merged.pop('_metadata')['review_status']=='proposed'
    validate(merged,n)
assert (snapshot/'review.html').is_file()
for v in read(data/'source_crops.json'):
    assert (snapshot/v['image']).is_file()
predictions=read(data/'all_proposals.json')
cases=[]
for proposal in predictions:
    unit=proposal['unit']
    expected=page_result(unit['pdf_page'],unit)
    expected['measurements'][0]['unit']='mm'
    cases.append(dict(id=unit['unit_id'],reviewed_by='synthetic MOCK fixture author',source_sha256=filehash(pdf),
        unit_id=unit['unit_id'],bbox=unit['bbox'],expected=expected))
labels=dict(version=1,cases=cases)
perfect=evaluate(labels,predictions)
assert all(v['f1']==1 for v in perfect['metrics'].values())
perturbed=deepcopy(predictions)
sample=perturbed[0]['extraction']
sample['nodes'][0]['article_id']='Z999'
sample['measurements'][0].update(value=53,unit='cm')
sample['tables'][0]['columns'][1]='Changed'
sample['tables'][0]['rows'][0]['cells'][1]='53'
sample['relations'][0]['predicate']='connects_to'
negative=evaluate(labels,perturbed[:-1])
assert negative['missing_predictions']==1 and all(v['f1']<1 for v in negative['metrics'].values())
dump(out/'mock_ground_truth.json',labels)
dump(out/'mock_benchmark.json',dict(perfect=perfect,perturbed=negative))
attempts=read(root/'work'/'attempts.json')
failed=[a for a in attempts if a['status']=='failed']
assert len(failed)==1
units=read(root/'units.json')
assert [u['unit_id'] for u in units]==[f'p{n:04}:full' for n in range(1,6)] # drawing pages are read whole
# 5 index + identity + 5 extractions + 5 relationship checks + 3 pilot-review attempts (one failed).
assert len(attempts)==19, len(attempts)
relations=read(data/'relationships.json')
# Verified by the (fake) reader, but the target 'V100' is not printed on the page: kept out of the graph.
assert relations and all(r['verification']=='supported' for r in relations)
assert all(not r['graph_eligible'] and 'target_article_ungrounded' in r['status_reasons'] for r in relations)
assert read(data/'graph_relationships.json')==[]
assert all(a in attempts for a in before_resume)
assert all(sum(other['job_id']==a['job_id'] for other in attempts)==1 for a in committed_before)
completed=[a for a in attempts if a['status']=='completed']
for a in completed:
    receipt=read(root/a['checkpoint'].removeprefix('run:'))
    assert all(filehash(root/p.removeprefix('run:'))==value for p,value in receipt['outputs'].items())
before=list(calls)
second=run()
assert calls==before and read(root/'work'/'attempts.json')==attempts
second_snapshot=Path(second.snapshot_directory)
for name in datasets:
    assert read(second_snapshot/'data'/f'{name}.json')==read(data/f'{name}.json')
replayed=FacadeExtractionAgent(out=out/'replay',reuse=second.output_directory,replay=True)(pdf_url=pdf.as_uri())
replay_snapshot=Path(replayed.snapshot_directory)
for name in ('relationships','graph_relationships'):
    assert read(replay_snapshot/'data'/f'{name}.json')==read(data/f'{name}.json')
for directory in ('reviews','verifications'):
    evidence=lambda snap:{p.name:p.read_bytes() for p in (snap/directory).glob('*.json')}
    assert evidence(second_snapshot) and evidence(second_snapshot)==evidence(replay_snapshot)
archive=zip_results(root,out/'MOCK_results.zip',second_snapshot)
with zipfile.ZipFile(archive) as z:
    assert z.testzip() is None
    manifest=__import__('json').loads(z.read('FILE_MANIFEST.json'))
    assert all(hashlib.sha256(z.read(name)).hexdigest()==sha for name,sha in manifest.items())
dump(out/'mock_summary.json',dict(mode='MOCK synthetic knowledge on actual five-page subset', coverage=coverage,
    calls=before, committed_attempts=len(completed), mock_failed_attempts=len(failed), preserved_commits_after_interrupt=len(committed_before), network_calls=0, datasets=datasets, snapshot=str(second_snapshot),
    archive=str(archive), archive_hashed_entries=len(manifest), replay_snapshot=str(replay_snapshot)))
print(read(out/'mock_summary.json'),flush=True)


