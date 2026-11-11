"""All tests use synthetic PDFs, saved data or a local fake CLI. No paid models."""
from pathlib import Path
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import copy
import json
import math
import shutil
import sys
import threading
import subprocess
import socket
import os
import pytest
import pymupdf
import polars as pl
import dspy
from facade_dspy.program import FacadeExtractionAgent,existing_bundle
from facade_dspy.storage import dump,read,filehash
from facade_dspy.queue import WorkQueue
from facade_dspy.schema import validate
from facade_dspy.source import acquire
from facade_dspy.budget import Budget,BudgetStopped
from facade_dspy.claude_tool import ClaudeCode,IncompleteRead,ToolFailure,decode_events
from facade_dspy.cli import zip_results


@pytest.fixture(autouse=True)
def no_external_network(monkeypatch):
    connect=socket.socket.connect
    def guarded(sock,address):
        if isinstance(address,tuple) and address[0] not in ['127.0.0.1','::1','localhost']:
            raise AssertionError('External network/model calls are forbidden in tests')
        return connect(sock,address)
    monkeypatch.setattr(socket.socket,'connect',guarded)


def pdf_file(path,pages=2):
    doc=pymupdf.open()
    for n in range(pages):
        page=doc.new_page();page.insert_text((50,70),f'Demo manufacturer / Demo system / 2026\nPage {n+1}\nA100 connector 52 mm; V100 screw, material unknown')
    doc.save(path);doc.close();return path


def page_result(number,unit=None):
    y=.12 if not unit else (unit['bbox'][1]+unit['bbox'][3])/2
    box=[.1,max(0,y-.03),.5,min(1,y+.03)]
    return dict(pdf_page=number,page_kind='detail',details=[dict(id='d',title='Synthetic fixture detail',drawing_refs=[],bbox=box)],
        nodes=[dict(id='a',detail_id='d',article_id='A100',kind='component_instance',label='Fixture connector',material=None,quantity=None,callout=None,bbox=box),
               dict(id='s',detail_id='d',article_id='V100',kind='fastener',label='Fixture screw',material=None,quantity=1,callout='2',bbox=box)],
        relations=[dict(source='a',predicate='fastened_by',target='s',basis='drawing_reading',condition='Fixture only',bbox=box,evidence='Leader from screw callout 2 to connector A100')],
        measurements=[dict(subject='a',property='width',value=52,unit=None,datum=None,condition=None,bbox=box)],
        tables=[dict(id='t',title='Synthetic table',columns=['Article','Value'],units=[None,None],rows=[dict(cells=['A100','52'],bbox=box)],footnotes=['Unit is unknown in this fixture'],bbox=box)],
        instructions=[],unresolved=['Material and engineering applicability remain unknown'])


class FakeBackend:
    def __init__(self,calls,mode=None):self.calls=calls;self.mode=mode
    def execute(self,task,prompt,schema,folder,heartbeat=lambda:None):
        self.calls.append(task)
        if task=='identity':output=dict(manufacturer_brand='Demo',legal_manufacturer=None,system_id='demo:facade',document_name='Synthetic fixture catalogue',edition='2026',copyright_notice=None,evidence_pages=[1,2],unresolved=[])
        elif task=='extraction':
            unit=read(folder/'scope.json')
            if self.mode=='truncate_once' and self.calls.count('extraction')==1:raise IncompleteRead('Synthetic truncation')
            output=dict(unit_id=unit['unit_id'],source_text=[],extraction=page_result(unit['pdf_page'],unit))
        elif task=='relation_check':
            unit=read(folder/'scope.json')
            verdict='unsupported' if self.mode=='relation_unsupported' else 'supported'
            output=dict(unit_id=unit['unit_id'],verdicts=[dict(claim_id=c['claim_id'],source=c['source'],predicate=c['predicate'],target=c['target'],
                verdict=verdict,reason='Test simulation',bbox=c['bbox']) for c in read(folder/'relations.json')])
        else:
            unit=read(folder/'scope.json')
            output=dict(unit_id=unit['unit_id'],findings=[dict(severity='error',message='Synthetic unsupported unit',bbox=unit['bbox'])] if self.mode=='review_error' else [],scope='Test simulation, not a real model review')
        return dict(output=output,reported_cost_usd=0,cache_hit=False,model='test fake; no actual model call')


@contextmanager
def serve_pdf(pdf):
    payload=pdf.read_bytes()
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200);self.send_header('Content-Type','application/pdf');self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload)
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:yield f'http://127.0.0.1:{server.server_port}/fixture.pdf?temporary=not-a-secret'
    finally:server.shutdown();server.server_close();thread.join()


def test_url_to_dspy_tool_checks_and_formats(tmp_path):
    pdf=pdf_file(tmp_path/'fixture.pdf');calls=[]
    with serve_pdf(pdf) as url:
        agent=FacadeExtractionAgent(out=tmp_path/'runs',budget_usd=10,backend_factory=lambda r,b:FakeBackend(calls))
        assert isinstance(agent,dspy.Module) and isinstance(agent.claude_code,dspy.Tool)
        result=agent(pdf_url=url);run=Path(result.output_directory)
        assert result.coverage['complete_proposal_pages']==[1,2]
        assert result.coverage['human_engineering_approved_records']==0
        # Drawing pages are read whole: one region per page, each with a relationship check.
        assert calls.count('extraction')==2 and calls.count('review')==2 and calls.count('relation_check')==2
        assert '?' not in read(run/'source.json')['source_url']
        assert (run/'data/components.json').exists() and not (run/'data/components.parquet').exists()
        assert len(read(run/'data/components.json'))==2
        assert '<html lang="en">' in (run/'review.html').read_text(encoding='utf-8')
        before=len(calls);again=agent(pdf_url=url);assert len(calls)==before
        assert read(run/'coverage.json')['complete_proposal_pages']==[1,2]
        assert filehash(pdf)==filehash(run/'source.pdf')
        archive=zip_results(run,tmp_path/'data.zip');assert archive.exists()


def test_profiling_reports_stages_without_changing_extraction_outputs(tmp_path):
    """Removing a stage hook or changing return values must break this contract."""
    pdf=pdf_file(tmp_path/'fixture.pdf')
    baseline=FacadeExtractionAgent(out=tmp_path/'baseline',budget_usd=10,
        backend_factory=lambda r,b:FakeBackend([]))(pdf_url=pdf.as_uri())
    agent=FacadeExtractionAgent(out=tmp_path/'profiled',budget_usd=10,profile=True,
        backend_factory=lambda r,b:FakeBackend([]))
    result=agent(pdf_url=pdf.as_uri())
    assert result.coverage==baseline.coverage
    for name in ['components','entities','relationships','measurements','table_rows']:
        assert read(Path(result.output_directory)/'data'/f'{name}.json')==read(Path(baseline.output_directory)/'data'/f'{name}.json')
    report=agent.profiling.report_path
    assert report.is_file() and not report.is_relative_to(Path(result.output_directory))
    summary=read(report.with_name('summary.json'))
    stages={s['stage'] for s in summary['stages']}
    assert {'source_acquisition','local_index','document_identity','region_planning','region_extraction',
            'structural_validation','source_verification','reconciliation_and_exports','queue_snapshot'}<=stages
    assert summary['outcome']=='processing_finished_with_coverage_report'
    assert all(s['duration_seconds']>=s['exclusive_seconds']>=0 for s in summary['stages'])
    assert summary['slow_functions']
    assert not any(f['function']=='[self]' for f in summary['slow_functions'])
    assert all((report.parent/s['profile_html']).is_file() for s in summary['stages'])
    assert (report.parent/'pipeline.html').is_file()
    assert 'https://fonts.googleapis.com' not in (report.parent/'pipeline.html').read_text(encoding='utf-8')
    assert (report.parent/'pipeline.pyisession').is_file()
    assert not (tmp_path/'baseline'/'profiles').exists()


def test_failed_local_stage_keeps_profile_and_original_exception(tmp_path,monkeypatch):
    """A failed indexing attempt must still leave timing/error evidence."""
    import facade_dspy.program as program
    pdf=pdf_file(tmp_path/'fixture.pdf')
    def fail_index(*args):raise RuntimeError('profiling fixture indexing failure')
    monkeypatch.setattr(program,'index',fail_index)
    agent=FacadeExtractionAgent(out=tmp_path/'runs',index_only=True,profile=True)
    with pytest.raises(RuntimeError,match='profiling fixture indexing failure'):
        agent(pdf_url=pdf.as_uri())
    summary=read(agent.profiling.report_path.with_name('summary.json'))
    assert summary['outcome']=='failed' and summary['error_type']=='RuntimeError'
    failed=next(s for s in summary['stages'] if s['stage']=='local_index')
    assert failed['status']=='failed' and failed['error_type']=='RuntimeError'
    assert agent.profiling.report_path.is_file()
    assert 'profiling fixture indexing failure' in (tmp_path/'runs'/'pipeline.log').read_text(encoding='utf-8')


def test_profile_report_failure_does_not_fail_pipeline(tmp_path,monkeypatch):
    """A disk/renderer failure must not turn successful indexing into a pipeline error."""
    from facade_dspy.profiling import PipelineProfiler
    pdf=pdf_file(tmp_path/'fixture.pdf',pages=1)
    def fail_report(*args):raise OSError('profiling fixture report write failure')
    monkeypatch.setattr(PipelineProfiler,'_write_report',fail_report)
    agent=FacadeExtractionAgent(out=tmp_path/'runs',index_only=True,profile=True)
    result=agent(pdf_url=pdf.as_uri())
    assert result.coverage['status']=='indexed_only'
    assert agent.profiling.report_path is None
    assert 'profiling fixture report write failure' in (tmp_path/'runs'/'pipeline.log').read_text(encoding='utf-8')


def test_project_dotenv_loads_without_overriding_environment(tmp_path,monkeypatch):
    import facade_dspy.program as program
    project=tmp_path/'project';project.mkdir()
    (project/'.env').write_text('ANTHROPIC_API_KEY=offline-fixture-value\n',encoding='utf-8')
    monkeypatch.setattr(program,'__file__',str(project/'facade_dspy'/'program.py'))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv('ANTHROPIC_API_KEY',raising=False)
    monkeypatch.delenv('PYTHON_DOTENV_DISABLED',raising=False)
    FacadeExtractionAgent(index_only=True)
    import os
    assert os.environ['ANTHROPIC_API_KEY']=='offline-fixture-value'
    monkeypatch.setenv('ANTHROPIC_API_KEY','existing-offline-fixture')
    FacadeExtractionAgent(index_only=True)
    assert os.environ['ANTHROPIC_API_KEY']=='existing-offline-fixture'


def test_pilot_error_blocks_scaling(tmp_path):
    pdf=pdf_file(tmp_path/'fixture.pdf');calls=[]
    agent=FacadeExtractionAgent(out=tmp_path/'runs',budget_usd=10,backend_factory=lambda r,b:FakeBackend(calls,'review_error'))
    result=agent(pdf_url=pdf.as_uri())
    assert result.coverage['status']=='pilot_source_check_found_errors'
    assert calls.count('extraction')==1
    # The pilot page was read whole, so its proposal exists; the review error stops all further pages.
    assert result.coverage['remaining_pages']==[2]


def test_truncated_region_is_split_never_promoted_to_page(tmp_path):
    pdf=pdf_file(tmp_path/'fixture.pdf');calls=[]
    agent=FacadeExtractionAgent(out=tmp_path/'runs',budget_usd=10,backend_factory=lambda r,b:FakeBackend(calls,'truncate_once'))
    result=agent(pdf_url=pdf.as_uri());run=Path(result.output_directory)
    assert result.coverage['complete_proposal_pages']==[]
    assert len(read(run/'units.json'))==4 # two whole pages, one split into two halves
    result=agent(pdf_url=pdf.as_uri())
    assert result.coverage['complete_proposal_pages']==[1,2]


def test_existing_page_replay_and_hash_mismatch(tmp_path):
    pdf=pdf_file(tmp_path/'fixture.pdf');saved=tmp_path/'saved';saved.mkdir()
    dump(saved/'document.json',dict(document_id='fixture',sha256=filehash(pdf),manufacturer_brand='Demo',system_id='demo:facade',edition='2026'))
    dump(saved/'full/pages/p001.json',page_result(1))
    # A valid region-shaped parse is deliberately outside full/pages and cannot become a full page.
    dump(saved/'region_proposals/p002-half.json',page_result(2))
    result=FacadeExtractionAgent(out=tmp_path/'runs',replay=True,reuse=saved)(pdf_url=pdf.as_uri())
    assert result.coverage['complete_proposal_pages']==[1]
    assert result.coverage['remaining_pages']==[2]
    assert not (Path(result.output_directory)/'cost_ledger.json').exists()
    with pytest.raises(ValueError,match='different source'):existing_bundle(saved,'bad-hash')


def test_portable_queue_receipts_and_changed_evidence(tmp_path):
    original=tmp_path/'original';original.mkdir();dump(original/'input.json',{'a':1})
    q=WorkQueue(original/'work');q.register('a','test','a',[original/'input.json'])
    def work(spec):dump(original/'out.json',{'ok':True});return {'outputs':[original/'out.json']}
    q.run('a',work);q.db.close()
    moved=tmp_path/'moved';shutil.copytree(original,moved)
    q=WorkQueue(moved/'work');assert q.audit()==[]
    dump(moved/'out.json',{'ok':False});assert q.audit()==['a'];q.db.close()


def test_budget_unknown_usage_stays_reserved(tmp_path):
    b=Budget(tmp_path,1,.6);ident=b.reserve('x',{'full_cli_input_token_count':None});b.settle(ident,{})
    with pytest.raises(BudgetStopped):b.reserve('y',{})
    b.settle(ident,{'total_cost_usd':.1,'usage':{'input_tokens':100}})
    assert b.summary()['reported_cost_usd']==.1
    assert b.summary()['provider_hard_cost_guarantee'] is False


def test_structural_guards(tmp_path):
    value=page_result(1);value['nodes'][0]['article_id']='K1013900'
    with pytest.raises(ValueError,match='Drawing ID'):validate(value,1)
    value=page_result(1);value['tables'][0]['rows'][0]['cells']=['A100']
    with pytest.raises(ValueError,match='table width'):validate(value,1)
    value=page_result(1);value['measurements'][0]['value']=float('inf')
    with pytest.raises(ValueError,match='Non-finite'):validate(value,1)


def test_cli_transport_read_receipts_cache_and_usage(tmp_path,monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY','test-fixture-not-a-real-key')
    script=tmp_path/'fake_cli.py'
    script.write_text('''import sys,json,pathlib
if '--version' in sys.argv:print('2.1.318 (fake CLI, no model)');sys.exit(0)
sys.stdin.read()
for i,name in enumerate(['source.png','focus.png']):
 pathlib.Path(name).read_bytes()
 print(json.dumps({'type':'assistant','message':{'content':[{'type':'tool_use','id':str(i),'name':'Read','input':{'file_path':name}}]}}))
 print(json.dumps({'type':'user','message':{'content':[{'type':'tool_result','tool_use_id':str(i),'is_error':False,'content':'fixture'}]}}))
print(json.dumps({'type':'result','is_error':False,'structured_output':{'ok':True},'total_cost_usd':0.01,'usage':{'input_tokens':20,'output_tokens':5}}))
''',encoding='utf-8')
    folder=tmp_path/'input';folder.mkdir();(folder/'source.png').write_bytes(b'fake');(folder/'focus.png').write_bytes(b'fake')
    b=Budget(tmp_path,1,.5);backend=ClaudeCode(tmp_path,b,[sys.executable,str(script)],accept_estimates=True)
    schema={'type':'object','properties':{'ok':{'type':'boolean'}},'required':['ok']}
    first=backend.execute('extraction','Read the fixture',schema,folder)
    second=backend.execute('extraction','Read the fixture',schema,folder)
    assert first['output']=={'ok':True} and second['cache_hit'] is True
    assert len(read(tmp_path/'cost_ledger.json'))==1
    assert b.summary()['reported_cost_usd']==.01
    result,reads,_=decode_events('not json\n'+json.dumps({'type':'result'}))
    assert result['type']=='result' and reads==set()
    cached=next((tmp_path/'tool_cache').glob('*.json'));tampered=read(cached);tampered['output']['ok']=False;dump(cached,tampered)
    with pytest.raises(RuntimeError,match='Cached structured output changed'):backend.execute('extraction','Read the fixture',schema,folder)
    assert len(read(tmp_path/'cost_ledger.json'))==1


@pytest.mark.parametrize('reject_reads',[False,True])
def test_tool_manifest_names_real_images_and_rejects_unread_output(tmp_path,monkeypatch,reject_reads):
    monkeypatch.setenv('ANTHROPIC_API_KEY','offline-test-fixture')
    script=tmp_path/'manifest_cli.py'
    script.write_text('''import sys,json,pathlib
if '--version' in sys.argv:print('2.1.318 (fake CLI, no model)');sys.exit(0)
prompt=sys.stdin.read()
line=next((s for s in prompt.splitlines() if s.startswith('TOOL_INPUT_MANIFEST: ')),None)
assert line is not None,'The agent did not supply actual image filenames'
manifest=json.loads(line.split(': ',1)[1])
for i,request in enumerate(manifest['required_image_reads']):
 p=pathlib.Path(request['file_path']);assert p.is_absolute();p.read_bytes()
 print(json.dumps({'type':'assistant','message':{'content':[{'type':'tool_use','id':str(i),'name':'Read','input':request}]}}))
 print(json.dumps({'type':'user','message':{'content':[{'type':'tool_result','tool_use_id':str(i),'is_error':REJECT,'content':'EISDIR: attempted directory read' if REJECT else 'fixture image'}]}}))
print(json.dumps({'type':'result','is_error':False,'structured_output':{'ok':True},'total_cost_usd':0.01,'usage':{'input_tokens':20,'output_tokens':5}}))
'''.replace('REJECT',repr(reject_reads)),encoding='utf-8')
    folder=tmp_path/'identity inputs';folder.mkdir();(folder/'source.png').write_bytes(b'fixture image')
    budget=Budget(tmp_path,1,.5);backend=ClaudeCode(tmp_path,budget,[sys.executable,str(script)],accept_estimates=True)
    schema={'type':'object','properties':{'ok':{'type':'boolean'}},'required':['ok']}
    if reject_reads:
        with pytest.raises(ToolFailure,match='source.png'):backend.execute('identity','Read all supplied images',schema,folder)
        assert not list((tmp_path/'tool_cache').glob('*.json'))
    else:
        reply=backend.execute('identity','Read all supplied images',schema,folder)
        assert reply['successful_image_reads']==['source.png']
    validation=read(next((tmp_path/'raw_responses').glob('*.read_validation.json')))
    assert validation['missing_image_files']==(['source.png'] if reject_reads else [])
    assert bool(validation['tool_errors'])==reject_reads
    assert budget.summary()['reported_cost_usd']==.01


@pytest.mark.parametrize('task',['identity','extraction'])
def test_missing_tool_images_stop_before_cost_reservation(tmp_path,monkeypatch,task):
    monkeypatch.setenv('ANTHROPIC_API_KEY','offline-test-fixture')
    folder=tmp_path/'inputs';folder.mkdir()
    if task=='extraction':(folder/'source.png').write_bytes(b'fixture image')
    budget=Budget(tmp_path,1,.5)
    backend=ClaudeCode(tmp_path,budget,[sys.executable,'-c',"print('2.1.318 (fake CLI)')"],accept_estimates=True)
    with pytest.raises(ToolFailure,match='no model process started'):
        backend.execute(task,'Read the images',{'type':'object'},folder)
    assert not (tmp_path/'cost_ledger.json').exists()


def test_authentication_failure_reports_provider_reason_without_retry(tmp_path,monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY','offline-test-fixture')
    script=tmp_path/'auth_error_cli.py'
    script.write_text('''import json,sys
if '--version' in sys.argv:print('2.1.318 (fake CLI)');sys.exit(0)
sys.stdin.read()
print(json.dumps({'type':'result','is_error':True,'terminal_reason':'api_error','api_error_status':401,
 'result':'Invalid API key · Fix external API key','total_cost_usd':0,'usage':{'input_tokens':0,'output_tokens':0}}))
''',encoding='utf-8')
    folder=tmp_path/'input';folder.mkdir();(folder/'source.png').write_bytes(b'fixture image')
    budget=Budget(tmp_path,1,.5);backend=ClaudeCode(tmp_path,budget,[sys.executable,str(script)],accept_estimates=True)
    with pytest.raises(ToolFailure,match='401.*Invalid API key'):
        backend.execute('identity','Read the image',{'type':'object'},folder)
    validation=read(next((tmp_path/'raw_responses').glob('*.read_validation.json')))
    assert validation['status']=='api_error' and validation['api_error_status']==401
    assert budget.summary()['reported_cost_usd']==0
    assert budget.summary()['outstanding_reservations_usd']==0
    assert len(read(tmp_path/'cost_ledger.json'))==1


def test_cross_document_boundaries_and_conflicting_claims(tmp_path):
    pdf=pdf_file(tmp_path/'fixture.pdf');saved=tmp_path/'saved';saved.mkdir()
    dump(saved/'document.json',dict(document_id='current',sha256=filehash(pdf),manufacturer_brand='Demo',system_id='demo:facade',edition='2026'))
    proposal=page_result(1);proposal['nodes'][0]['material']='aluminium';dump(saved/'full/pages/p001.json',proposal)
    for brand,name in [('Demo','same_system'),('Another brand','other_manufacturer')]:
        base=tmp_path/name;base.mkdir()
        dump(base/'document.json',dict(document_id=name,sha256='synthetic-other-source',manufacturer_brand=brand,system_id='demo:facade',edition='2025'))
        dump(base/'components.json',[dict(article_id='A100',observations=[dict(label='Prior fixture claim',material='steel',pdf_page=1,edition='2025')])])
    result=FacadeExtractionAgent(out=tmp_path/'runs',replay=True,reuse=saved,compare=[tmp_path/'same_system',tmp_path/'other_manufacturer'])(pdf_url=pdf.as_uri())
    run=Path(result.output_directory);links=read(run/'data/cross_document_links.json');comparison=read(run/'data/cross_document_comparison.json')
    assert len(links)==1 and links[0]['compatibility'] is None
    material=next(r for r in comparison if r['property_relationship']=='material')
    assert material['assessment']=='conflict' and material['old_claim']==['steel'] and material['new_evidence']==['aluminium']
    assert any(q['category']=='cross_document_scope' for q in read(run/'data/questions.json'))


def test_own_complete_exports_can_be_replayed(tmp_path):
    pdf=pdf_file(tmp_path/'fixture.pdf');calls=[]
    live=FacadeExtractionAgent(out=tmp_path/'first',budget_usd=10,backend_factory=lambda r,b:FakeBackend(calls))(pdf_url=pdf.as_uri())
    replay=FacadeExtractionAgent(out=tmp_path/'second',reuse=live.output_directory,replay=True)(pdf_url=pdf.as_uri())
    assert replay.coverage['complete_proposal_pages']==[1,2]
    assert not (Path(replay.output_directory)/'cost_ledger.json').exists()


def test_cli_url_index_only_never_requires_a_model(tmp_path):
    pdf=pdf_file(tmp_path/'fixture.pdf')
    with serve_pdf(pdf) as url:
        result=subprocess.run([sys.executable,'-m','facade_dspy',url,'--index-only','--out',str(tmp_path/'runs'),
            '--export-zip',str(tmp_path/'indexed.zip')],capture_output=True,text=True,encoding='utf-8',timeout=45)
    assert result.returncode==0,result.stderr
    output=json.loads(result.stdout)
    assert output['coverage']['status']=='indexed_only'
    assert Path(output['results_zip']).exists()


@pytest.mark.skipif(os.name!='nt',reason='Windows PowerShell 5.1 native stderr regression')
def test_windows_powershell_stop_does_not_abort_on_layout_notice(tmp_path):
    shell=Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe'
    if not shell.exists():pytest.skip('Windows PowerShell 5.1 is unavailable')
    pdf=pdf_file(tmp_path/'fixture.pdf',pages=1);out=tmp_path/'runs';log=tmp_path/'console.log'
    quote=lambda p:"'"+str(p).replace("'","''")+"'"
    command="$ErrorActionPreference='Stop'\n& "+quote(sys.executable)+" -u -B -m facade_dspy "+quote(pdf)+" --index-only --out "+quote(out)+" 2>&1 | Tee-Object -FilePath "+quote(log)+"\nexit $LASTEXITCODE"
    completed=subprocess.run([str(shell),'-NoProfile','-NonInteractive','-Command',command],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=50)
    assert completed.returncode==0,completed.stderr
    report=json.loads(completed.stdout)
    assert report['coverage']['indexed_pages']==1
    assert (out/'pipeline.log').exists()
    assert 'local_index' in (out/'pipeline.log').read_text(encoding='utf-8')


def test_local_failure_has_traceback_and_stage_log(tmp_path,monkeypatch,capsys):
    import facade_dspy.program as program
    pdf=pdf_file(tmp_path/'fixture.pdf',pages=1)
    def broken_index(run,queue):
        print('Third-party diagnostic',file=sys.stderr)
        raise RuntimeError('Injected local indexing failure')
    monkeypatch.setattr(program,'index',broken_index)
    out=tmp_path/'runs'
    with pytest.raises(RuntimeError,match='Injected local indexing failure'):
        FacadeExtractionAgent(out=out,index_only=True)(pdf_url=pdf.as_uri())
    text=(out/'pipeline.log').read_text(encoding='utf-8')
    assert 'Third-party diagnostic' in text and 'Traceback' in text
    assert 'Injected local indexing failure' in text and 'local_index' in text
    assert 'Third-party diagnostic' not in capsys.readouterr().err


def test_single_page_runner_local_scope_and_error_logs(tmp_path):
    pdf=pdf_file(tmp_path/'source.pdf',pages=2);original=filehash(pdf)
    script=Path(__file__).parents[1]/'test_pdf_page.py'
    for number,success in [(2,True),(3,False)]:
        result=subprocess.run([sys.executable,'-u','-B',str(script),'--pdf',str(pdf),'--page',str(number),
            '--out',str(tmp_path/'isolated'),'--index-only','--profile'],capture_output=True,text=True,encoding='utf-8',timeout=50)
        assert result.stderr==''
        summary=json.loads(result.stdout);folder=Path(summary['test_folder'])
        assert (result.returncode==0)==success
        if success:
            run=Path(summary['output_directory'])
            assert Path(summary['profile_report']).is_file()
            assert summary['coverage']['indexed_pages']==1
            with pymupdf.open(run/'source.pdf') as doc:assert len(doc)==1
            assert read(run/'original_page_provenance.json')['original_pdf_page']==2
            assert not (run/'cost_ledger.json').exists()
            assert not (run/'raw_responses').exists()
            assert 'local_index' in (run/'pipeline.log').read_text(encoding='utf-8')
            import zipfile
            with zipfile.ZipFile(summary['results_zip']) as z:
                assert 'pipeline.log' in z.namelist() and z.testzip() is None
        else:
            log=(folder/'console.log').read_text(encoding='utf-8')
            assert 'Traceback' in log and 'requested physical PDF page' in log
        assert filehash(pdf)==original


def test_existing_em070_schema_is_backward_compatible():
    path=Path(__file__).parents[1]/'examples/EM070_existing_proposal.json'
    record=read(path);actual=validate({k:v for k,v in record.items() if k!='_metadata'},43)
    assert any(n['article_id']=='EM070' for n in actual['nodes'])


def test_bad_download_and_size_limit(tmp_path):
    nonpdf=tmp_path/'not.pdf';nonpdf.write_text('<html>Login page</html>',encoding='utf-8')
    with pytest.raises(ValueError,match='did not return a PDF'):acquire(nonpdf.as_uri(),tmp_path/'out')
    pdf=pdf_file(tmp_path/'fixture.pdf')
    with pytest.raises(ValueError,match='size limit'):acquire(pdf.as_uri(),tmp_path/'out',max_bytes=1)
