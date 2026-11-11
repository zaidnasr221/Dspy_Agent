"""Opt-in wall-clock sampling and stage reports; never inspect model payloads."""
from collections import defaultdict
from datetime import datetime,timezone
from pathlib import Path
from time import perf_counter
import html
import json
import re
import traceback
import uuid


class PipelineProfiler:
    def __init__(self,out,enabled=False,mode='full',trace=False):
        self.out=Path(out);self.enabled=enabled;self.mode=mode;self.report_path=None;self.active=False
        self.trace=trace
        if enabled:
            try:
                from pyinstrument import Profiler
            except ImportError as exc:
                raise RuntimeError('Profiling requires Pyinstrument: python -m pip install ".[profile]"') from exc
            self.profiler_class=Profiler

    def start(self):
        if not self.enabled:return
        self.report_path=None;self.active=False
        try:
            self.folder=self.out/'profiles'/uuid.uuid4().hex
            self.folder.mkdir(parents=True,exist_ok=False)
            self.records=[];self.stack=[];self.sessions=[]
            self.started_at=datetime.now(timezone.utc).isoformat();self.started=perf_counter()
            self.total=self.profiler_class(async_mode='disabled')
            self.total.start(target_description='Facade DSPy pipeline ('+self.mode+')')
            self.active=True
        except Exception:
            print('[profiling] could not start; continuing without profiling',flush=True);traceback.print_exc()

    def call(self,stage,function,*args,**kwargs):
        if self.trace:
            import mlflow
            with mlflow.start_span(name=stage) as span:
                result=self._call(stage,function,*args,**kwargs)
                if stage=='catalogue_support':
                    span.set_outputs(result)
                if isinstance(result,dict) and 'tool_receipt' in result.get('metadata',{}):
                    receipt=result['metadata']['tool_receipt']
                    span.set_attributes({key:receipt[key] for key in ('backend','model','reasoning_effort','reported_cost_usd','cache_hit')
                                         if receipt.get(key) is not None})
                return result
        return self._call(stage,function,*args,**kwargs)

    def _call(self,stage,function,*args,**kwargs):
        if not self.active:return function(*args,**kwargs)
        profiler=self.profiler_class(async_mode='disabled')
        try:
            profiler.start(target_description='Facade stage: '+stage)
        except Exception:
            print('[profiling] stage sampler could not start:',stage,flush=True);traceback.print_exc()
            return function(*args,**kwargs)
        row=dict(id=len(self.records)+1,stage=stage,parent_id=self.stack[-1]['id'] if self.stack else None,
                 start_seconds=perf_counter()-self.started,status='completed',error_type=None,child_seconds=0)
        self.records.append(row);self.stack.append(row)
        started=perf_counter()
        try:
            return function(*args,**kwargs)
        except BaseException as exc:
            row.update(status='failed',error_type=type(exc).__name__)
            raise
        finally:
            duration=perf_counter()-started
            self.stack.pop()
            if self.stack:self.stack[-1]['child_seconds']+=duration
            row.update(duration_seconds=duration,exclusive_seconds=max(0,duration-row['child_seconds']),
                       profile_html=f'stage_{row["id"]:05}_{stage}.html')
            # Stopping/reporting must not replace the pipeline's result or original exception.
            try:
                profiler.stop();self.sessions.append((row,profiler))
            except Exception:
                print('[profiling] stage sampler failed:',stage,flush=True);traceback.print_exc()

    def finish(self,outcome,error_type=None):
        if not self.active:return
        try:
            self.total.stop()
            duration=perf_counter()-self.started
            self._write_report(outcome,error_type,duration)
            self.report_path=self.folder/'index.html'
            print('[profiling] report:',self.report_path,flush=True)
        except Exception:
            print('[profiling] report generation failed; pipeline outcome is unchanged',flush=True)
            traceback.print_exc()
        finally:
            self.active=False

    def _write_report(self,outcome,error_type,duration):
        from pyinstrument.renderers import JSONRenderer
        self._write_profile(self.total,self.folder/'pipeline.html')
        self.total.last_session.save(self.folder/'pipeline.pyisession')
        samples=json.loads(self.total.output(JSONRenderer(show_all=True)))
        (self.folder/'pipeline.json').write_text(json.dumps(samples,indent=2),encoding='utf-8')
        for row,profiler in self.sessions:
            self._write_profile(profiler,self.folder/row['profile_html'])
        functions={}
        def visit(frame):
            if not frame:return
            # Pyinstrument emits synthetic [self] nodes; attribute those samples
            # to the real parent function instead of presenting them as functions.
            children=[c for c in frame.get('children',[]) if c['function']!='[self]']
            key=(frame['function'],frame['file_path'],frame['line_no'])
            item=functions.setdefault(key,dict(function=key[0],file=key[1],line=key[2],sampled_self_seconds=0))
            item['sampled_self_seconds']+=max(0,frame['time']-sum(c['time'] for c in children))
            for child in children:visit(child)
        visit(samples.get('root_frame'))
        slow=sorted(functions.values(),key=lambda f:-f['sampled_self_seconds'])[:30]
        aggregate=defaultdict(lambda:dict(calls=0,duration_seconds=0,exclusive_seconds=0,failed_calls=0))
        for row in self.records:
            a=aggregate[row['stage']];a['calls']+=1;a['duration_seconds']+=row['duration_seconds']
            a['exclusive_seconds']+=row['exclusive_seconds'];a['failed_calls']+=row['status']=='failed'
        expected={'source_acquisition','local_index','checkpoint_audit','document_identity','region_planning',
                  'saved_result_import','region_extraction','response_schema_validation','normalization',
                  'structural_validation','source_verification','image_preparation',
                  'reconciliation_and_exports','queue_snapshot'}
        unassigned=max(0,duration-sum(r['duration_seconds'] for r in self.records if r['parent_id'] is None))
        summary=dict(mode=self.mode,outcome=outcome,error_type=error_type,started_at=self.started_at,
            duration_seconds=duration,sample_count=samples['sample_count'],sample_interval_seconds=.001,
            outside_stage_seconds=unassigned,
            cpu_seconds=samples['cpu_time'],stages=self.records,
            stage_totals=[dict(stage=k,**v) for k,v in sorted(aggregate.items(),key=lambda kv:-kv[1]['exclusive_seconds'])],
            slow_functions=slow,not_observed_stages=sorted(expected-set(aggregate)),
            limitations=[
                'Measured wall time includes I/O and subprocess waiting, not just CPU work.',
                'Backend CLI subprocess internals and remote model execution are not sampled.',
                'Inclusive stage times overlap; exclusive times subtract immediate measured child stages.',
                'Uninstrumented controller work and profiler overhead remain in the whole-pipeline view.',
                'Sub-millisecond functions may have no samples. Stage timers still record elapsed time.',
                'Reconciliation, knowledge graph assembly and exports share the existing export function.',
                'Agent initialization, page-isolation setup, final results ZIP and report rendering are outside this profile.',
                'Unobserved stages are not measured as zero. No performance optimization is applied.'
            ])
        (self.folder/'summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
        def esc(value):return html.escape(str(value))
        rows=''.join('<tr><td>'+esc(r['stage'])+'</td><td>'+str(r['calls'])+'</td><td>'+f"{r['duration_seconds']:.4f}"+'</td><td>'+f"{r['exclusive_seconds']:.4f}"+'</td><td>'+str(r['failed_calls'])+'</td></tr>' for r in summary['stage_totals'])
        funcs=''.join('<tr><td>'+esc(f['function'])+'</td><td>'+f"{f['sampled_self_seconds']:.4f}"+'</td><td><code>'+esc(f['file'])+':'+str(f['line'])+'</code></td></tr>' for f in slow)
        options='<option value="pipeline.html">Whole pipeline</option>'+''.join('<option value="'+r['profile_html']+'">'+str(r['id'])+' · '+esc(r['stage'])+' · '+f"{r['duration_seconds']:.4f}"+' s · '+esc(r['status'])+'</option>' for r in self.records)
        report='''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Facade Agent performance profile</title>
<style>body{font:16px/1.6 system-ui;margin:24px;color:#172b42;background:#f4f7fb}main{max-width:1400px;margin:auto}section{background:white;border:1px solid #d5dfeb;border-radius:12px;padding:20px;margin:18px 0}h1,h2{line-height:1.3}button,select,input{font:inherit;padding:8px;max-width:100%}table{border-collapse:collapse;width:100%;font-size:14px}td,th{padding:9px;text-align:left;border-bottom:1px solid #d5dfeb;vertical-align:top}th{cursor:pointer;background:#edf2ff}code{overflow-wrap:anywhere}.scroll{overflow:auto}iframe{width:100%;height:760px;border:1px solid #d5dfeb;background:white}.note{color:#52647a}a{color:#1455b5}@media(max-width:600px){body{margin:12px}section{padding:12px}iframe{height:650px}}</style></head><body><main>
<h1>Facade Agent performance profile</h1>'''
        report+='<p>Mode: <strong>'+esc(self.mode)+'</strong> · Outcome: <strong>'+esc(outcome)+'</strong> · Wall time: <strong>'+f'{duration:.4f}'+' s</strong> · CPU time: '+f"{samples['cpu_time']:.4f}"+' s · Samples: '+str(samples['sample_count'])+'</p>'
        report+='<p class="note">Measured execution only. This is performance evidence, not engineering verification. Click table headers to sort; select a stage to explore its sampled call tree.</p>'
        if summary['stage_totals']:
            largest=summary['stage_totals'][0]
            report+='<p>Largest measured exclusive stage: <strong>'+esc(largest['stage'])+'</strong> ('+f"{largest['exclusive_seconds']:.4f}"+' s). Outside named stage boundaries: '+f'{unassigned:.4f}'+' s; inspect the whole-pipeline tree for controller/setup work and sampling overhead.</p>'
        report+='<section><h2>Stages, ranked by exclusive elapsed time</h2><div class="scroll"><table id="stages"><thead><tr><th>Stage</th><th>Calls</th><th>Inclusive seconds</th><th>Exclusive seconds</th><th>Failed calls</th></tr></thead><tbody>'+rows+'</tbody></table></div></section>'
        report+='<section><h2>Interactive Pyinstrument call tree</h2><label for="view">Profile </label><select id="view">'+options+'</select> <a id="open" href="pipeline.html" target="_blank" rel="noopener">Open selected profile</a><iframe id="profile" title="Interactive Pyinstrument profile" src="pipeline.html"></iframe></section>'
        report+='<section><h2>Slow functions by sampled self time</h2><p class="note">Self time excludes sampled child calls. This is a sampling estimate; short calls may not appear. Native-library and subprocess-wait frames can dominate.</p><label for="filter">Find a function or file </label><input id="filter" type="search"><div class="scroll"><table id="functions"><thead><tr><th>Function</th><th>Sampled self seconds</th><th>Location</th></tr></thead><tbody>'+funcs+'</tbody></table></div></section>'
        report+='<section><h2>Measurement boundaries</h2><ul>'+''.join('<li>'+esc(x)+'</li>' for x in summary['limitations'])+'</ul><p>Stages not observed: '+esc(', '.join(summary['not_observed_stages']) or 'None')+'</p><p><a href="summary.json">Stage timing data</a> · <a href="pipeline.json">Sampled call tree JSON</a> · <a href="pipeline.pyisession">Reusable Pyinstrument session</a></p></section>'
        report+='''</main><script>
document.getElementById('view').onchange=e=>{document.getElementById('profile').src=e.target.value;document.getElementById('open').href=e.target.value};
document.getElementById('filter').oninput=e=>{const q=e.target.value.toLowerCase();document.querySelectorAll('#functions tbody tr').forEach(r=>r.hidden=!r.textContent.toLowerCase().includes(q))};
document.querySelectorAll('table').forEach(t=>t.querySelectorAll('th').forEach((h,i)=>{h.tabIndex=0;h.setAttribute('role','button');h.title='Sort this column';h.onclick=()=>{const rows=[...t.tBodies[0].rows],ascending=h.dataset.order!=='asc';rows.sort((a,b)=>{const x=a.cells[i].textContent,y=b.cells[i].textContent;const n=Number(x),m=Number(y);return (Number.isNaN(n)||Number.isNaN(m)?x.localeCompare(y):n-m)*(ascending?1:-1)});t.tBodies[0].append(...rows);t.querySelectorAll('th').forEach(c=>c.removeAttribute('aria-sort'));h.dataset.order=ascending?'asc':'desc';h.setAttribute('aria-sort',ascending?'ascending':'descending')};h.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();h.click()}}}));
</script></body></html>'''
        (self.folder/'index.html').write_text(report,encoding='utf-8')

    @staticmethod
    def _write_profile(profiler,path):
        # The upstream UI preloads Google Fonts; use installed fallback fonts offline.
        content=re.sub(r'https://fonts\.googleapis\.com/[^"\x27]+','data:text/css,',profiler.output_html())
        path.write_text(content,encoding='utf-8')
