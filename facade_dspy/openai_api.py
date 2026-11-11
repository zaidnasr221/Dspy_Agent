"""OpenAI Responses vision backend for bounded catalogue extraction."""
import base64
import json
import os
import sys
from socket import gaierror
from time import sleep
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .storage import dump, objecthash
from .tool_protocol import AuthenticationFailure, IncompleteRead, InsufficientCredits, RateLimitFailure, ToolFailure, redact


class OpenAIResponses:
    def __init__(self, run, budget, model):
        self.run, self.budget, self.model = run, budget, model.removeprefix('openai/')
        if self.model != 'gpt-6.1-sol':
            raise ValueError('The direct OpenAI backend budgets gpt-6.1-sol only')
        self.key = os.environ.get('OPENAI_API_KEY', '').strip()
        if not self.key:
            raise AuthenticationFailure('Set OPENAI_API_KEY for the OpenAI backend')

    def request(self, endpoint, payload):
        req = Request('https://api.openai.com/v1/responses' + endpoint, json.dumps(payload).encode(),
                      {'Authorization': 'Bearer ' + self.key, 'Content-Type': 'application/json'})
        for delay in (2, 4, 8, 16, None):
            try:
                return self._send(req)
            except URLError as error:
                safe_to_retry = isinstance(error.reason, gaierror) or (
                    endpoint == '/input_tokens' and 'EOF occurred' in str(error.reason)
                )
                if delay is None or not safe_to_retry:
                    raise
                sleep(delay)

    @staticmethod
    def _send(req):
        try:
            with urlopen(req, timeout=300) as response:
                return json.load(response)
        except HTTPError as error:
            message = redact(error.read().decode(errors='replace')[:2000])
            if error.code in (401, 403):
                raise AuthenticationFailure('OpenAI authentication or access rejected: ' + message) from None
            if 'insufficient_quota' in message:
                raise InsufficientCredits('OpenAI quota rejected the request: ' + message) from None
            if error.code == 429:
                raise RateLimitFailure('OpenAI rate limit: ' + message) from None
            raise ToolFailure(f'OpenAI HTTP {error.code}: {message}') from None

    def execute(self, task, prompt, schema, folder, heartbeat):
        mlflow = sys.modules.get('mlflow')
        if mlflow is None or mlflow.get_current_active_span() is None:
            return self._execute(task, prompt, schema, folder, heartbeat)
        from mlflow.tracing.attachments import Attachment

        with mlflow.start_span(name='OpenAI '+task, span_type='LLM') as span:
            files = {}
            for path in sorted(folder.iterdir()):
                if path.suffix == '.png':
                    files[path.name] = Attachment.from_file(path)
                elif path.suffix in {'.txt','.json'}:
                    files[path.name] = redact(path.read_text(encoding='utf-8'))
            span.set_inputs(dict(task=task, prompt=redact(prompt), output_schema=schema, evidence_files=files))
            result = self._execute(task, prompt, schema, folder, heartbeat)
            span.set_outputs(result['output'])
            span.set_attributes({'mlflow.llm.model':self.model, 'mlflow.llm.provider':'openai',
                                 'reasoning_effort':'low', 'reported_cost_usd':result.get('reported_cost_usd'),
                                 'mlflow.chat.tokenUsage':result.get('usage') or {}})
            return result

    def _execute(self, task, prompt, schema, folder, heartbeat):
        files = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix in {'.png', '.json', '.txt'})
        content = [{'type': 'input_text', 'text': prompt + '\nReturn one JSON object matching this schema: '
                    + json.dumps(schema, ensure_ascii=False)}]
        for path in files:
            if path.suffix == '.png':
                content.append({'type': 'input_text', 'text': path.name})
                content.append({'type': 'input_image', 'detail': 'high',
                                'image_url': 'data:image/png;base64,' + base64.b64encode(path.read_bytes()).decode()})
            else:
                content.append({'type': 'input_text', 'text': path.name + '\n' + path.read_text(encoding='utf-8')})
        base = dict(model=self.model, input=[dict(role='user', content=content)],
                    reasoning={'effort': 'low'}, text={'format': {'type': 'json_object'}})
        maximum = {'identity': 1500, 'extraction': 16000, 'relation_check': 8000, 'review': 5000}[task]
        request_hash = objecthash(base)
        heartbeat()
        count = self.request('/input_tokens', {k: base[k] for k in ('model', 'input', 'text')})['input_tokens']
        if count > 272000:
            raise ToolFailure('OpenAI input exceeds the standard-rate token threshold')
        # Bound input at the uncached rate and output at the enforced token limit.
        reserve = (count * 2 + maximum * 10) / 1_000_000
        attempt = self.budget.reserve(request_hash, dict(backend='OpenAI Responses', model=self.model,
                                      input_tokens=count, max_output_tokens=maximum), amount=reserve)
        try:
            response = self.request('', dict(base, max_output_tokens=maximum, store=False))
        except (AuthenticationFailure, InsufficientCredits, RateLimitFailure):
            self.budget.release_unused(attempt, 'OpenAI rejected the request before generation')
            raise
        except URLError as error:
            if isinstance(error.reason, gaierror):
                self.budget.release_unused(attempt, 'DNS failed before connecting to OpenAI')
            raise
        usage = response.get('usage')
        cost = None
        if usage:
            cached = (usage.get('input_tokens_details') or {}).get('cached_tokens', 0)
            cost = ((usage['input_tokens'] - cached) * 2 + cached * .10 + usage['output_tokens'] * 10) / 1_000_000
            self.budget.settle(attempt, dict(total_cost_usd=cost, usage=usage))
        raw = self.run / 'raw_responses' / (attempt + '.json')
        dump(raw, response)
        if response.get('status') != 'completed':
            raise IncompleteRead(f'OpenAI {task} incomplete: {response.get("incomplete_details")}')
        outputs = [part['text'] for item in response.get('output', []) if item['type'] == 'message'
                   for part in item.get('content', []) if part['type'] == 'output_text']
        if not outputs:
            raise ToolFailure('OpenAI completed without JSON output')
        return dict(output=json.loads(''.join(outputs)), raw_response=raw.relative_to(self.run).as_posix(),
                    usage=usage, reported_cost_usd=cost, cache_hit=False,
                    backend='openai', model=self.model, reasoning_effort='low')
