"""A read-only Claude Code subprocess tool; never calls another model provider."""
from pathlib import Path
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from .storage import dump,read,filehash,objecthash
from .model import DEFAULT_MODELS
from .tool_protocol import ToolFailure,IncompleteRead,redact,required_read_files


def decode_events(text,folder=None):
    events=[]
    for line in text.splitlines():
        try:obj=json.loads(line)
        except json.JSONDecodeError:continue
        if isinstance(obj,dict):events.append(obj)
    result=next((e for e in reversed(events) if e.get('type')=='result'),None)
    reads={};success=set()
    for e in events:
        message=e.get('message',e)
        for item in message.get('content',[]) if isinstance(message.get('content'),list) else []:
            if item.get('type')=='tool_use' and item.get('name')=='Read':
                path=Path(item.get('input',{}).get('file_path',''))
                if path.name and (folder is None or (folder/path).resolve().parent==folder):
                    reads[item.get('id')]=path.name
            if item.get('type')=='tool_result' and not item.get('is_error',False):success.add(item.get('tool_use_id'))
    return result,{reads[i] for i in success if i in reads},events


class ClaudeCode:
    def __init__(self,run,budget,command=None,model=None,cli_budget=.15,timeout=240,accept_estimates=False):
        if not accept_estimates:raise ValueError('Live Claude Code requires explicit acceptance of estimated CLI cost controls')
        if not 0<cli_budget<budget.reserve_amount:raise ValueError('CLI stop threshold must be below the reserved headroom')
        if not os.environ.get('ANTHROPIC_API_KEY'):raise ValueError('Claude Code bare mode requires a fresh locally configured ANTHROPIC_API_KEY; subscription login is not used in this mode')
        self.run=run;self.budget=budget;self.model=model or DEFAULT_MODELS['claude-code'];self.cli_budget=cli_budget;self.timeout=timeout
        local=Path(__file__).parents[1]/'.local_setup/claude/node_modules/@anthropic-ai/claude-code/bin'/('claude.exe' if os.name=='nt' else 'claude')
        found=shutil.which('claude')
        self.command=command or ([str(local)] if local.exists() else [found] if found else None)
        if not self.command:raise FileNotFoundError('Claude Code is not installed. Install/authenticate it, or use replay mode.')
        if isinstance(self.command,str):self.command=[self.command]
        if Path(self.command[0]).suffix.casefold() in ['.cmd','.bat']:
            raise ValueError('On Windows use the native claude.exe or node plus the CLI JavaScript entry; prompts never go through a shell')
        version=subprocess.run(self.command+['--version'],capture_output=True,text=True,encoding='utf-8',timeout=20,check=True)
        self.version=version.stdout.strip()
        numbers=re.search(r'(\d+)\.(\d+)\.(\d+)',self.version)
        if not numbers or tuple(map(int,numbers.groups()))<(2,1,248):raise ValueError('Claude Code >=2.1.248 is required for restricted reads')

    def execute(self,task,prompt,schema,folder,heartbeat=lambda:None):
        folder=Path(folder).resolve()
        images={p.name:filehash(p) for p in folder.glob('*.png')}
        inputs={p.name:filehash(p) for p in folder.iterdir() if p.is_file()}
        required=required_read_files(task,images)
        if not images or required-set(inputs):
            raise ToolFailure('Required tool input files are missing; no model process started: '+(', '.join(sorted(required-set(inputs))) or 'no PNG images supplied'))
        manifest=dict(working_directory=str(folder),
            available_files=[dict(file_path=str(folder/name)) for name in sorted(inputs)],
            required_image_reads=[dict(file_path=str(folder/name)) for name in sorted(required&images.keys())],
            required_reads=[dict(file_path=str(folder/name)) for name in sorted(required)])
        prompt+='\nUse Read with the exact file_path values below. Read every required file before answering. Do not Read the directory or guess filenames.\nTOOL_INPUT_MANIFEST: '+json.dumps(manifest,ensure_ascii=False)
        payload=dict(task=task,prompt=prompt,schema=schema,inputs=inputs,model=self.model,version=self.version,
                     tool_policy='restricted Read only; bare; fresh session',cli_stop_usd=self.cli_budget,max_turns=6)
        request_hash=objecthash(payload);cache=self.run/'tool_cache'/f'{request_hash}.json'
        if cache.exists():
            saved=read(cache)
            if saved.get('request_hash')!=request_hash or saved.get('output_sha256')!=objecthash(saved.get('output')):
                raise ToolFailure('Cached structured output changed; no new generation was started')
            from .storage import safe_path
            original=safe_path(self.run,saved['raw_response'])
            if not original.exists() or filehash(original)!=saved.get('raw_sha256'):raise ToolFailure('Cached raw evidence changed; no new generation was started')
            return {**saved,'cache_hit':True}
        preflight=dict(prompt_utf8_bytes=len(prompt.encode()),image_count=len(images),
            known_input_file_bytes=sum(p.stat().st_size for p in folder.iterdir() if p.is_file()),
            full_cli_input_token_count=None,method='Local size checks only; Claude Code system/tool tokens are not exposed before execution')
        attempt=self.budget.reserve(request_hash,preflight)
        rawdir=self.run/'raw_responses';rawdir.mkdir(exist_ok=True)
        raw=rawdir/f'{attempt}.jsonl';stderr_path=rawdir/f'{attempt}.stderr.txt'
        argv=self.command+['--bare','--restricted','-p','--output-format','stream-json','--verbose',
            '--json-schema',json.dumps(schema,separators=(',',':')),'--model',self.model,'--tools','Read',
            '--allowedTools','Read','--permission-mode','dontAsk','--strict-mcp-config','--mcp-config','{"mcpServers":{}}',
            '--no-session-persistence','--max-turns','6','--max-budget-usd',str(self.cli_budget)]
        dump(rawdir/f'{attempt}.request.json',payload)
        # Do not put prompts, URLs or credentials into shell text. Each call has a fresh isolated input directory.
        env=os.environ.copy();env.pop('CLAUDECODE',None)
        env['CLAUDE_CODE_SKIP_PROMPT_HISTORY']='1';env['DISABLE_NONESSENTIAL_TRAFFIC']='1'
        proc=None;first=True;start=time.monotonic();stdout='';stderr=''
        try:
            proc=subprocess.Popen(argv,cwd=folder,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                text=True,encoding='utf-8',errors='replace',shell=False,env=env,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            while True:
                try:
                    stdout,stderr=proc.communicate(input=prompt if first else None,timeout=15);break
                except subprocess.TimeoutExpired:
                    first=False;heartbeat()
                    if time.monotonic()-start>self.timeout:
                        proc.kill();stdout,stderr=proc.communicate();raise TimeoutError('Claude Code timed out; reservation retained unless a usage receipt is available')
        except OSError:
            if proc is None:self.budget.cancel_before_launch(attempt)
            raise
        finally:
            if proc and proc.poll() is None:proc.kill();stdout,stderr=proc.communicate()
            # Preserve incomplete replies before any parsing; redact token-like secrets in diagnostic text.
            raw.write_text(redact(stdout),encoding='utf-8');stderr_path.write_text(redact(stderr),encoding='utf-8')
            result,successful_reads,events=decode_events(stdout,folder)
            if result:self.budget.settle(attempt,result)
            errors=[]
            for event in events:
                blocks=event.get('message',event).get('content',[])
                for block in blocks if isinstance(blocks,list) else []:
                    if block.get('type')=='tool_result' and block.get('is_error'):
                        errors.append(dict(tool_use_id=block.get('tool_use_id'),message=redact(str(block.get('content','')))[:1000]))
            missing=sorted(required-successful_reads)
            api_status=result.get('api_error_status') if result else None
            api_error=bool(result and (api_status or result.get('terminal_reason')=='api_error'))
            validation=rawdir/f'{attempt}.read_validation.json'
            dump(validation,dict(required_files=sorted(required),successful_files=sorted(successful_reads),
                missing_files=missing,required_image_files=sorted(required&images.keys()),
                successful_image_files=sorted(successful_reads&images.keys()),missing_image_files=sorted(set(missing)&images.keys()),
                tool_errors=errors,raw_response=raw.name,api_error_status=api_status,
                status='api_error' if api_error else 'rejected_unread_source' if missing else 'image_read_receipts_present_not_engineering_approval'))
        if api_error:
            reason=redact(str(result.get('result','Request rejected')))[:600]
            raise ToolFailure('Claude Code API error (HTTP '+str(api_status or 'unknown')+'): '+reason+'; inspect '+str(raw))
        if result is None or proc.returncode or result.get('is_error'):
            raise IncompleteRead('Incomplete Claude Code response retained: '+raw.name)
        output=result.get('structured_output')
        if not isinstance(output,dict):raise IncompleteRead('No structured output; raw response retained')
        if missing:raise ToolFailure('Source file Read receipts missing: '+', '.join(missing)+'; inspect '+str(validation))
        reply=dict(output=output,raw_response='raw_responses/'+raw.name,request_hash=request_hash,
                   usage=result.get('usage'),model_usage=result.get('modelUsage'),reported_cost_usd=result.get('total_cost_usd'),
                   successful_image_reads=sorted(successful_reads&images.keys()),model=self.model,cache_hit=False)
        reply['read_validation']='raw_responses/'+validation.name
        reply.update(raw_sha256=filehash(raw),output_sha256=objecthash(output))
        dump(cache,reply);return reply
