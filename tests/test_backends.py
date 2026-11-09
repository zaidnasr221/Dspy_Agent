"""Provider switching and CLI protocol checks; synthetic fixtures, no API traffic."""
from pathlib import Path
import json
import os
import socket
import subprocess
import sys
import pytest
from facade_dspy.program import FacadeExtractionAgent
from facade_dspy.budget import Budget
from facade_dspy.claude_tool import ToolFailure,IncompleteRead
from facade_dspy.storage import read,dump
from test_agent import pdf_file,page_result


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args,**kwargs):raise AssertionError('Network forbidden in provider tests')
    monkeypatch.setattr(socket.socket,'connect',denied)
    monkeypatch.setenv('OPENAI_API_KEY','offline-openai-fixture-not-a-real-key')
    monkeypatch.setenv('ANTHROPIC_API_KEY','offline-anthropic-fixture-not-a-real-key')
    for name in ('FACADE_BACKEND', 'FACADE_MODEL'):
        monkeypatch.setenv(name, '')
        monkeypatch.delenv(name)


def fake_cli(tmp_path,backend='opencode',mode='ok'):
    script=tmp_path/(backend+'_'+mode+'.py')
    fixture=tmp_path/'fixture.json'
    dump(fixture,page_result(1))
    code=r'''
import json,sys,pathlib,os,socket
socket.socket.connect=lambda *a,**k: (_ for _ in ()).throw(AssertionError('Network forbidden'))
BACKEND=__BACKEND__;MODE=__MODE__
if '--version' in sys.argv:
 print('1.19.0' if BACKEND=='opencode' else '2.1.318 (fake Claude Code)');sys.exit(0)
prompt=sys.stdin.read();folder=pathlib.Path.cwd()
if BACKEND=='opencode':
 assert 'run' in sys.argv and '--pure' in sys.argv and '--format' in sys.argv
 config=json.loads(os.environ['OPENCODE_CONFIG_CONTENT'])
 assert config['enabled_providers']==['openai']
 assert config['model']==config['small_model']==sys.argv[sys.argv.index('--model')+1]
 assert config['permission']['*']=='deny' and isinstance(config['permission']['read'],dict)
 assert config['provider']['openai']['options']['apiKey']=='{env:OPENAI_API_KEY}'
 assert config['share']=='disabled' and config['autoupdate'] is False
 assert os.environ['OPENAI_API_KEY']=='offline-openai-fixture-not-a-real-key'
 assert 'ANTHROPIC_API_KEY' not in os.environ
 for k in ['XDG_CONFIG_HOME','XDG_DATA_HOME','XDG_CACHE_HOME','XDG_STATE_HOME']:
  assert pathlib.Path(os.environ[k]).is_dir()
manifest=json.loads(next(l.split(': ',1)[1] for l in prompt.splitlines() if l.startswith('TOOL_INPUT_MANIFEST: ')))
if MODE=='error' or MODE=='error_review' and (folder/'proposal.json').is_file():
 print(json.dumps({'type':'error','error':{'name':'APIError','data':{'statusCode':401,'message':'Invalid key '+os.environ['OPENAI_API_KEY']}}}));sys.exit(1)
if MODE=='stderr_error':
 print('HTTP 401 Unauthorized: Incorrect API key provided: '+os.environ['OPENAI_API_KEY'],file=sys.stderr);sys.exit(1)
if MODE in ('billing','rate_limit','billing_partial') or (
 MODE=='billing_review' and (folder/'proposal.json').is_file()
) or (MODE=='billing_identity' and folder.name=='identity_inputs') or (
 MODE=='billing_extraction' and (folder/'scope.json').is_file() and not (folder/'proposal.json').exists()
):
 if MODE=='billing_partial':
  print(json.dumps({'type':'step_start','part':{'id':'started'}}))
 message='Rate limit exceeded' if MODE=='rate_limit' else 'You have no credits remaining. Add credits.'
 print(json.dumps({'type':'error','error':{'name':'APIError','data':{
  'statusCode':429,'isRetryable':True,'message':message}}}));sys.exit(1)
if BACKEND=='opencode':print(json.dumps({'type':'step_start','part':{'id':'start','messageID':'m'}}))
for i,item in enumerate(manifest['required_image_reads']):
 p=pathlib.Path(item['file_path']);p.read_bytes()
 if MODE=='missing_read':continue
 actual=str(folder.parent/p.name) if MODE=='wrong_path' else str(p)
 if BACKEND=='opencode':
  print(json.dumps({'type':'tool_use','part':{'id':'read'+str(i),'messageID':'m','tool':'read','state':{'status':'completed','input':{'filePath':actual},'output':'Image read successfully'}}}))
 else:
  print(json.dumps({'type':'assistant','message':{'content':[{'type':'tool_use','id':str(i),'name':'Read','input':{'file_path':actual}}]}}))
  print(json.dumps({'type':'user','message':{'content':[{'type':'tool_result','tool_use_id':str(i),'is_error':False,'content':'fixture'}]}}))
if (folder/'scope.json').exists():
 unit=json.loads((folder/'scope.json').read_text())
 if (folder/'proposal.json').exists():output={'unit_id':unit['unit_id'],'findings':[],'scope':'Offline fixture, not engineering review'}
 else:
  value=json.loads(pathlib.Path(__FIXTURE__).read_text());value['pdf_page']=unit['pdf_page']
  output={'unit_id':unit['unit_id'],'source_text':[],'extraction':value}
elif 'manufacturer_brand' in prompt or folder.name=='identity_inputs':
 output={'manufacturer_brand':'Demo','legal_manufacturer':None,'system_id':'demo:facade','document_name':'Offline fixture','edition':'2026','copyright_notice':None,'evidence_pages':[1],'unresolved':[]}
else:output={'ok':True}
if MODE=='bad_schema':output={'ok':'not a boolean'}
text=json.dumps(output) if MODE!='malformed' else '{incomplete'
if BACKEND=='opencode':
 print(json.dumps({'type':'text','part':{'id':'text','messageID':'m','text':text}}))
 if MODE!='missing_finish':
  part={'id':'finish','messageID':'m','reason':'length' if MODE=='length' else 'stop','cost':0 if MODE=='zero_cost' else .01,'tokens':{'input':20,'output':5,'reasoning':0,'cache':{'read':0,'write':0}}}
  print(json.dumps({'type':'step_finish','part':part}))
  if MODE=='duplicate_finish':print(json.dumps({'type':'step_finish','part':part}))
  if MODE=='unfinished_tail':print(json.dumps({'type':'step_start','part':{'id':'later-start','messageID':'later'}}))
else:print(json.dumps({'type':'result','is_error':False,'structured_output':output,'total_cost_usd':.01,'usage':{'input_tokens':20,'output_tokens':5}}))
'''
    script.write_text(code.replace('__BACKEND__',repr(backend)).replace('__MODE__',repr(mode)).replace('__FIXTURE__',repr(str(fixture))),encoding='utf-8')
    return [sys.executable,str(script)]


def test_central_selection_and_environment_precedence(monkeypatch):
    from facade_dspy.model import resolve_model
    assert resolve_model().backend=='opencode'
    assert resolve_model().model=='openai/gpt-6.1-sol'
    monkeypatch.setenv('FACADE_BACKEND','opencode')
    monkeypatch.setenv('FACADE_MODEL','openai/configured-test-model')
    config=resolve_model()
    assert config.backend=='opencode' and config.api_key_env=='OPENAI_API_KEY'
    assert config.model=='openai/configured-test-model'
    assert resolve_model('opencode','openai/explicit-test-model').model=='openai/explicit-test-model'
    assert resolve_model('claude-code','sonnet').model=='sonnet'
    with pytest.raises(ValueError):resolve_model('opencode','anthropic/claude-sonnet-5-5')
    with pytest.raises(ValueError):resolve_model('missing-backend')


def test_project_dotenv_selects_opencode_without_constructing_live_backend(tmp_path,monkeypatch):
    import facade_dspy.program as program
    project=tmp_path/'project';(project/'facade_dspy').mkdir(parents=True)
    (project/'.env').write_text('OPENAI_API_KEY=offline-openai-fixture-not-a-real-key\n',encoding='utf-8')
    monkeypatch.setattr(program,'__file__',str(project/'facade_dspy/program.py'))
    monkeypatch.delenv('OPENAI_API_KEY',raising=False)
    monkeypatch.delenv('PYTHON_DOTENV_DISABLED',raising=False)
    agent=program.FacadeExtractionAgent(index_only=True)
    assert agent.model_config.backend=='opencode'
    assert agent.model=='openai/gpt-6.1-sol' and agent.backend is None
    assert os.environ.get('OPENAI_API_KEY')=='offline-openai-fixture-not-a-real-key'


def test_opencode_missing_inputs_do_not_reserve_cost(tmp_path):
    from facade_dspy.opencode_tool import OpenCode
    budget=Budget(tmp_path,1,.5)
    folder=tmp_path/'inputs';folder.mkdir();(folder/'source.png').write_bytes(b'fixture')
    adapter=OpenCode(tmp_path,budget,command=fake_cli(tmp_path),accept_estimates=True)
    with pytest.raises(ToolFailure,match='no model process started'):
        adapter.execute('extraction','Read source',{'type':'object'},folder)
    assert not (tmp_path/'cost_ledger.json').exists()


@pytest.mark.parametrize('value', [None, '  ', 'contains whitespace'])
def test_opencode_missing_key_fails_before_starting_cli(tmp_path,monkeypatch,value):
    from facade_dspy.opencode_tool import OpenCode
    if value is None:
        monkeypatch.delenv('OPENAI_API_KEY')
    else:
        monkeypatch.setenv('OPENAI_API_KEY', value)
    with pytest.raises(ValueError,match='OPENAI_API_KEY'):
        OpenCode(tmp_path,Budget(tmp_path,1,.5),command=['this-must-not-start.exe'],accept_estimates=True)


@pytest.mark.parametrize('backend',['claude-code','opencode'])
def test_switch_backends_preserves_shared_pipeline_and_outputs(tmp_path,backend,monkeypatch):
    pdf=pdf_file(tmp_path/'fixture.pdf',pages=1)
    command=fake_cli(tmp_path,'opencode' if backend=='opencode' else 'claude')
    if backend=='opencode':
        monkeypatch.setenv('OPENCODE_CONFIG_CONTENT', '{"model":"openai/stale-test-model"}')
    agent=FacadeExtractionAgent(out=tmp_path/'runs',backend=None if backend=='opencode' else backend,command=command,
                                budget_usd=1,accept_cli_estimates=True)
    result=agent(pdf_url=pdf.as_uri());run=Path(result.output_directory)
    assert result.coverage['complete_proposal_pages']==[1]
    assert result.coverage['human_engineering_approved_records']==0
    assert read(run/'pilot_validation.json')['passed']
    assert {e['article_id'] for e in read(run/'data/entities.json')}=={'A100','V100'}
    assert {e['provenance']['reading_method'] for e in read(run/'data/entities.json')}=={
        'OpenCode/OpenAI visual reading' if backend=='opencode' else 'Claude Code visual reading'}
    assert len(read(run/'data/relationships.json'))==2
    assert read(run/'model_selection.json')['backend']==backend
    expected_model = 'openai/gpt-6.1-sol' if backend=='opencode' else 'claude-sonnet-5-5'
    requests = [read(path) for path in (run/'raw_responses').glob('*.request.json')]
    assert {request['task'] for request in requests} == {'identity', 'extraction', 'review'}
    assert {request['model'] for request in requests} == {expected_model}
    assert read(run/'cost_summary.json')['reported_cost_usd']==pytest.approx(.05)
    for dataset in ['components','entities','relationships','measurements','table_rows']:
        for ext in ['json','parquet','csv']:assert (run/'data'/f'{dataset}.{ext}').exists()
    assert (run/'review.html').is_file()
    before=list((run/'raw_responses').glob('*.jsonl'))
    assert agent(pdf_url=pdf.as_uri()).coverage==result.coverage
    assert list((run/'raw_responses').glob('*.jsonl'))==before
    other='claude-code' if backend=='opencode' else 'opencode'
    with pytest.raises(ValueError,match='fresh output'):
        FacadeExtractionAgent(out=tmp_path/'runs',backend=other,budget_usd=1,
                              command=command,accept_cli_estimates=True)(pdf_url=pdf.as_uri())


@pytest.mark.parametrize('mode',['ok','duplicate_finish','missing_read','wrong_path','missing_finish','unfinished_tail','bad_schema','malformed','length','error','stderr_error','zero_cost'])
def test_opencode_protocol_cache_failures_and_costs(tmp_path,mode):
    from facade_dspy.opencode_tool import OpenCode
    folder=tmp_path/'inputs';folder.mkdir()
    for name in ['source.png','focus.png']:(folder/name).write_bytes(b'offline image fixture')
    budget=Budget(tmp_path,1,.5)
    backend=OpenCode(tmp_path,budget,command=fake_cli(tmp_path,mode=mode),accept_estimates=True)
    schema={'type':'object','properties':{'ok':{'type':'boolean'}},'required':['ok'],'additionalProperties':False}
    if mode in ['ok','duplicate_finish','zero_cost']:
        result=backend.execute('extraction','Read all images',schema,folder)
        assert result['output']=={'ok':True}
        assert result['successful_image_reads']==['focus.png','source.png']
        assert backend.execute('extraction','Read all images',schema,folder)['cache_hit']
        assert len(read(tmp_path/'cost_ledger.json'))==1
    else:
        match = 'OpenAI authentication failed.*OPENAI_API_KEY' if mode in ('error', 'stderr_error') else None
        with pytest.raises(ToolFailure, match=match):
            backend.execute('extraction','Read all images',schema,folder)
        assert not list((tmp_path/'tool_cache').glob('*.json'))
    assert (tmp_path/'raw_responses').is_dir()
    validation = read(next((tmp_path/'raw_responses').glob('*.read_validation.json')))
    if mode in ('error', 'stderr_error'):
        assert validation['status'] == 'authentication_error'
    if mode in ['zero_cost','missing_finish','unfinished_tail','error','stderr_error']:
        assert budget.summary()['outstanding_reservations_usd']==.5
    else:assert budget.summary()['reported_cost_usd']==.01
    assert len(read(tmp_path/'cost_ledger.json'))==1
    for p in (tmp_path/'raw_responses').iterdir():
        assert 'offline-openai-fixture-not-a-real-key' not in p.read_text(encoding='utf-8')


def test_opencode_auth_failure_during_review_reaches_the_caller(tmp_path):
    pdf = pdf_file(tmp_path / 'fixture.pdf', pages=1)
    agent = FacadeExtractionAgent(out=tmp_path / 'runs',
        command=fake_cli(tmp_path, mode='error_review'),
        model='openai/offline-test-model', budget_usd=1, accept_cli_estimates=True)
    with pytest.raises(ToolFailure, match='OpenAI authentication failed.*OPENAI_API_KEY') as failure:
        agent(pdf_url=pdf.as_uri())
    assert 'offline-openai-fixture-not-a-real-key' not in str(failure.value)
    assert not (agent.run / 'publication.json').exists()


@pytest.mark.parametrize('mode,kind,held',[
    ('billing','InsufficientCredits',0),
    ('rate_limit','RateLimitFailure',0),
    ('billing_partial','InsufficientCredits',.5),
])
def test_opencode_429_classification_and_reservations(tmp_path,mode,kind,held):
    from facade_dspy.opencode_tool import OpenCode
    folder=tmp_path/'inputs';folder.mkdir()
    for name in ('source.png','focus.png'):(folder/name).write_bytes(b'image')
    budget=Budget(tmp_path,1,.5)
    backend=OpenCode(tmp_path,budget,command=fake_cli(tmp_path,mode=mode),accept_estimates=True)
    with pytest.raises(ToolFailure) as failure:
        backend.execute('extraction','Read images',{'type':'object'},folder)
    assert type(failure.value).__name__==kind
    assert budget.summary()['outstanding_reservations_usd']==held
    assert len(read(budget.path))==1
    if kind=='InsufficientCredits':
        assert 'Add credits' in str(failure.value) and 'same PDF and --out' in str(failure.value)


@pytest.mark.parametrize('mode,stage',[
    ('billing_identity','document_identity'),
    ('billing_extraction','region_extraction'),
    ('billing_review','selected_source_verification'),
])
def test_billing_stops_run_and_next_invocation_resumes(tmp_path,mode,stage):
    from facade_dspy.tool_protocol import InsufficientCredits
    pdf=pdf_file(tmp_path/'fixture.pdf',pages=1)
    options=dict(out=tmp_path/'runs',budget_usd=1,accept_cli_estimates=True)
    first=FacadeExtractionAgent(**options,command=fake_cli(tmp_path,mode=mode))
    with pytest.raises(InsufficientCredits):
        first(pdf_url=pdf.as_uri())
    run=first.run
    before=read(run/'work'/'attempts.json')
    failed=[a for a in before if a['status']=='failed']
    assert len(failed)==1
    jobs=read(run/'work'/'queue.json')['jobs']
    assert next(j for j in jobs if j['id']==failed[0]['job_id'])['stage']==stage
    assert not any(j['status']=='running' for j in jobs)
    assert Budget(run,1,.5).summary()['outstanding_reservations_usd']==0

    resumed=FacadeExtractionAgent(**options,command=fake_cli(tmp_path))
    result=resumed(pdf_url=pdf.as_uri())
    assert result.coverage['complete_proposal_pages']==[1]
    after=read(run/'work'/'attempts.json')
    for attempt in before:
        assert attempt in after
        if attempt['status']=='completed':
            assert sum(a['job_id']==attempt['job_id'] for a in after)==1
    assert [a['status'] for a in after if a['job_id']==failed[0]['job_id']]==['failed','completed']


def test_both_entry_points_expose_backend_and_model():
    project=Path(__file__).parents[1]
    for argv in [['-m','facade_dspy','--help'],[str(project/'test_pdf_page.py'),'--help']]:
        result=subprocess.run([sys.executable,'-B',*argv],capture_output=True,text=True,encoding='utf-8',cwd=project,timeout=40)
        assert result.returncode==0
        assert '--backend' in result.stdout and '--model' in result.stdout
