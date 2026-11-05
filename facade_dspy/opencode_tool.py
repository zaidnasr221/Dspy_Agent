"""Read-only OpenCode/OpenAI adapter, independent of the Claude Code transport."""
from pathlib import Path
import json
import math
import os
import re
import shutil
import subprocess
import time
from jsonschema import Draft202012Validator,ValidationError
from .model import DEFAULT_MODELS,resolve_model
from .storage import dump,read,filehash,objecthash,safe_path
from .tool_protocol import ToolFailure,IncompleteRead,redact


def find_command():
    """Resolve an installed native binary without invoking Windows shell shims."""
    local=Path(__file__).parents[1]/'.local_setup/opencode/node_modules/opencode-ai/bin'
    names=[local/('opencode.exe' if os.name=='nt' else 'opencode')]
    found=shutil.which('opencode')
    if found:
        p=Path(found)
        if p.suffix.lower() in ('.cmd','.bat','.ps1'):
            names.insert(0,p.parent/'node_modules/opencode-ai/bin/opencode.exe')
        else:names.insert(0,p)
    return [str(p) for p in names if p.is_file()][:1] or None


def runtime_environment(folder,files,state,model):
    """Exact read allowlist, isolated CLI state, OpenAI key referenced only via env."""
    root=next((p for p in (folder,*folder.parents) if (p/'.git').exists()),folder)
    allow={'*':'deny'}
    for p in files:
        for key in (str(p),p.as_posix(),str(p.relative_to(root)),p.relative_to(root).as_posix()):allow[key]='allow'
    permission={'*':'deny','read':allow}
    config=dict(model=model,small_model=model,enabled_providers=['openai'],autoupdate=False,
        share='disabled',snapshot=False,instructions=[],plugin=[],mcp={},
        permission=permission,
        compaction={'auto':False},
        provider={'openai':{'options':{'apiKey':'{env:OPENAI_API_KEY}','baseURL':'https://api.openai.com/v1','maxRetries':0}}},
        agent={'facade-reader':{'description':'Read source images and return proposed catalogue data',
                               'mode':'primary','steps':6,'permission':permission},'title':{'disable':True}})
    env=os.environ.copy()
    for name in list(env):
        if name.startswith('OPENCODE_') or name in ('ANTHROPIC_API_KEY','CLAUDECODE'):env.pop(name,None)
    for name,sub in [('XDG_CONFIG_HOME','config'),('XDG_DATA_HOME','data'),('XDG_CACHE_HOME','cache'),('XDG_STATE_HOME','state')]:
        p=state/sub;p.mkdir(parents=True,exist_ok=True);env[name]=str(p)
    env.update(OPENCODE_CONFIG_CONTENT=json.dumps(config),OPENCODE_DISABLE_PROJECT_CONFIG='1',
        OPENCODE_DISABLE_EXTERNAL_SKILLS='1',OPENCODE_DISABLE_CLAUDE_CODE='1',
        OPENCODE_DISABLE_AUTOUPDATE='1',OPENCODE_DISABLE_MODELS_FETCH='1',OPENCODE_PURE='1',
        OPENCODE_CONFIG_DIR=str(state/'config'))
    return env


def decode_events(text,folder):
    events=[]
    for line in text.splitlines():
        try:value=json.loads(line)
        except json.JSONDecodeError:continue
        if isinstance(value,dict):events.append(value)
    reads=set();errors=[];steps={};starts=set();texts={}
    for event in events:
        part=event.get('part') or {};kind=event.get('type')
        if kind=='error':errors.append(event.get('error') or {'message':'OpenCode error'})
        elif kind=='step_start':starts.add(part.get('messageID',part.get('id')))
        elif kind=='step_finish':steps[part.get('id',str(len(steps)))]=part
        elif kind=='text':texts[part.get('id',str(len(texts)))]=part
        elif kind=='tool_use':
            state=part.get('state') or {};tool=part.get('tool')
            if tool!='read':errors.append({'message':'Unexpected tool used: '+str(tool)});continue
            if state.get('status')=='error':errors.append({'message':state.get('error','Read failed')});continue
            if state.get('status')=='completed':
                value=state.get('input',{}).get('filePath','')
                path=Path(value);path=path if path.is_absolute() else folder/path
                if path.resolve().parent==folder:reads.add(path.name)
    ended={p.get('messageID',p.get('id')) for p in steps.values()}
    last=next(reversed(steps.values()),None)
    terminal=last if last and last.get('reason') in ('stop','end_turn','length','max-tokens') and starts.issubset(ended) else None
    final_id=terminal.get('messageID') if terminal else None
    final_text=''.join(p.get('text','') for p in texts.values() if not final_id or p.get('messageID')==final_id).strip()
    total=None;usage={'steps':[p.get('tokens') for p in steps.values()]}
    complete=bool(terminal) # A token-limit finish still carries real billed usage.
    costs=[p.get('cost') for p in steps.values()]
    if complete and starts.issubset(ended) and costs and all(isinstance(c,(int,float)) and not isinstance(c,bool) and math.isfinite(c) and c>=0 for c in costs):
        # A zero price for a paid OpenAI generation may mean unknown catalogue pricing.
        if all(c>0 or isinstance(p.get('tokens'),dict) and p['tokens'].get('input')==0 and p['tokens'].get('output')==0 for c,p in zip(costs,steps.values())):
            total=sum(costs)
    return dict(events=events,reads=reads,errors=errors,terminal=terminal,text=final_text,
                total_cost_usd=total,usage=usage)


class OpenCode:
    def __init__(self,run,budget,command=None,model=None,timeout=240,accept_estimates=False):
        if not accept_estimates:raise ValueError('OpenCode requires acceptance of estimated costs; its CLI has no per-call dollar cap')
        if not os.environ.get('OPENAI_API_KEY'):raise ValueError('OpenCode requires a locally configured OPENAI_API_KEY')
        self.run=Path(run);self.budget=budget;self.timeout=timeout
        self.model=resolve_model('opencode',model or DEFAULT_MODELS['opencode']).model
        self.command=([command] if isinstance(command,str) else command) or find_command()
        if not self.command:raise FileNotFoundError('OpenCode is not installed; supply --backend-executable or install opencode-ai')
        if Path(self.command[0]).suffix.lower() in ('.cmd','.bat','.ps1'):raise ValueError('Use a native opencode executable; shell shims are not supported')
        env=os.environ.copy();env['OPENCODE_DISABLE_AUTOUPDATE']='1';env['OPENCODE_DISABLE_MODELS_FETCH']='1'
        version=subprocess.run(self.command+['--version'],capture_output=True,text=True,encoding='utf-8',timeout=20,check=True,env=env)
        self.version=version.stdout.strip();numbers=re.search(r'(\d+)\.(\d+)\.(\d+)',self.version)
        if not numbers or not (1,19,0)<=tuple(map(int,numbers.groups()))<(2,0,0):
            raise ValueError('This adapter supports the OpenCode 1.19.x/1.x JSON protocol (>=1.19,<2)')

    def execute(self,task,prompt,schema,folder,heartbeat=lambda:None):
        Draft202012Validator.check_schema(schema)
        folder=Path(folder).resolve();files=sorted(p for p in folder.iterdir() if p.is_file())
        if any(p.resolve().parent!=folder for p in files):raise ToolFailure('Tool input links must not escape the registered folder')
        inputs={p.name:filehash(p) for p in files};images={p.name for p in files if p.suffix=='.png'}
        required=images if task=='identity' else {'source.png','focus.png'}
        if not images or required-images:raise ToolFailure('Required images missing; no model process started')
        manifest=dict(working_directory=str(folder),available_files=[dict(file_path=str(p)) for p in files],
                      required_image_reads=[dict(file_path=str(folder/n)) for n in sorted(required)])
        prompt+='\nUse the OpenCode read tool (equivalent to Read) with filePath set to each exact file_path below. Read ALL required images. Return only one JSON object matching FINAL_JSON_SCHEMA, without markdown.\nTOOL_INPUT_MANIFEST: '+json.dumps(manifest)+'\nFINAL_JSON_SCHEMA: '+json.dumps(schema)
        payload=dict(backend='opencode',provider='openai',task=task,prompt=prompt,schema=schema,inputs=inputs,
                     model=self.model,version=self.version,tool_policy='exact read allowlist; pure; isolated state; max 6 steps')
        key=objecthash(payload);cache=self.run/'tool_cache'/f'{key}.json'
        if cache.exists():
            saved=read(cache);raw=safe_path(self.run,saved['raw_response'])
            if saved.get('request_hash')!=key or saved.get('output_sha256')!=objecthash(saved.get('output')):
                raise ToolFailure('Cached structured output changed; no generation started')
            if not raw.exists() or filehash(raw)!=saved.get('raw_sha256'):raise ToolFailure('Cached raw evidence changed; no generation started')
            return {**saved,'cache_hit':True}
        preflight=dict(backend='OpenCode/OpenAI',prompt_utf8_bytes=len(prompt.encode()),image_count=len(images),
            known_input_file_bytes=sum(p.stat().st_size for p in files),full_cli_input_token_count=None,
            method='Local sizes only; CLI internal tokens and an exact pre-count are unavailable; no per-call CLI dollar cap')
        attempt=self.budget.reserve(key,preflight)
        rawdir=self.run/'raw_responses';rawdir.mkdir(exist_ok=True)
        raw=rawdir/f'{attempt}.jsonl';validation=rawdir/f'{attempt}.read_validation.json'
        dump(rawdir/f'{attempt}.request.json',payload)
        state=self.run/'opencode_runtime'/attempt
        env=runtime_environment(folder,files,state,self.model)
        argv=self.command+['run','--pure','--format','json','--model',self.model,'--agent','facade-reader','--title','Facade '+task]
        proc=None;stdout='';stderr='';first=True;started=time.monotonic()
        try:
            proc=subprocess.Popen(argv,cwd=folder,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                text=True,encoding='utf-8',errors='replace',shell=False,env=env,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            while True:
                try:stdout,stderr=proc.communicate(input=prompt if first else None,timeout=15);break
                except subprocess.TimeoutExpired:
                    first=False;heartbeat()
                    if time.monotonic()-started>self.timeout:
                        proc.kill();stdout,stderr=proc.communicate();raise TimeoutError('OpenCode timed out; reservation retained without complete usage')
        except OSError:
            if proc is None:self.budget.cancel_before_launch(attempt)
            raise
        finally:
            if proc and proc.poll() is None:proc.kill();stdout,stderr=proc.communicate()
            raw.write_text(redact(stdout),encoding='utf-8')
            raw.with_suffix('.stderr.txt').write_text(redact(stderr),encoding='utf-8')
            result=decode_events(stdout,folder);self.budget.settle(attempt,result)
            missing=sorted(required-result['reads'])
            dump(validation,dict(required_image_files=sorted(required),successful_image_files=sorted(result['reads']),
                missing_image_files=missing,tool_errors=json.loads(redact(json.dumps(result['errors']))),
                raw_response=raw.name,status='tool_error' if result['errors'] else 'rejected_unread_source' if missing else 'image_read_receipts_present_not_engineering_approval',
                complete_terminal_step=bool(result['terminal']),reported_cost_usd=result['total_cost_usd']))
        if result['errors']:raise ToolFailure('OpenCode error: '+redact(json.dumps(result['errors']))[:700]+'; inspect '+str(raw))
        if proc.returncode or not result['terminal'] or result['terminal'].get('reason') not in ('stop','end_turn'):
            raise IncompleteRead('Incomplete OpenCode terminal response retained: '+str(raw))
        if missing:raise ToolFailure('Source image Read receipts missing: '+', '.join(missing)+'; inspect '+str(validation))
        try:
            text=result['text']
            if text.startswith('```json') and text.endswith('```'):text=text[7:-3].strip()
            output=json.loads(text)
            if not isinstance(output,dict):raise ValueError('Expected a JSON object')
            Draft202012Validator(schema).validate(output)
        except (ValueError,ValidationError) as exc:
            raise IncompleteRead('OpenCode returned invalid structured output; raw response retained: '+str(raw)) from exc
        reply=dict(output=output,raw_response='raw_responses/'+raw.name,request_hash=key,usage=result['usage'],
            model_usage={self.model:dict(costUSD=result['total_cost_usd'],usage=result['usage'])},
            reported_cost_usd=result['total_cost_usd'],successful_image_reads=sorted(result['reads']),
            model=self.model,backend='opencode',cache_hit=False,read_validation='raw_responses/'+validation.name)
        reply.update(raw_sha256=filehash(raw),output_sha256=objecthash(output));dump(cache,reply)
        return reply
