"""Keep OpenCode's session retries away from OpenAI billing rejections."""
from contextlib import contextmanager
from http.client import HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
from threading import Thread
from uuid import uuid4


BILLING = re.compile(
    r'\b(?:insufficient_quota|billing_hard_limit_reached|billing_not_active|'
    r'(?:no|zero) credits? remaining|insufficient (?:API )?credits?|'
    r'exceeded your current quota|quota exceeded\. check your plan and billing details)\b', re.I)
RATE_LIMIT = re.compile(r'\brate_limit_exceeded\b|\brate limit(?: reached| exceeded)\b', re.I)
BILLING_ERROR = {
    'code': 'insufficient_quota', 'type': 'insufficient_quota',
    'message': 'OpenAI billing rejected the request. Add API credits.',
}


class RetryGuard:
    def __init__(self):
        self.billing_rejected = False
        self.forwarded_success = False

    def rewrite_sse(self, frame):
        data = b'\n'.join(line[5:].lstrip() for line in frame.splitlines() if line.startswith(b'data:'))
        try:
            event = json.loads(data)
        except ValueError:
            return frame
        if not isinstance(event, dict):
            return frame
        kind = event.get('type')
        response = event.get('response') or {}
        error = response.get('error') if kind == 'response.failed' and isinstance(response, dict) else event
        if kind in ('error', 'response.failed') and BILLING.search(json.dumps(error)):
            self.billing_rejected = True
            # OpenCode maps this OpenAI SSE error code to a non-retryable error.
            return b'data: ' + json.dumps(dict(BILLING_ERROR, type='error', param=None)).encode() + b'\n\n'
        if kind not in ('error', 'response.failed', 'response.created', 'response.in_progress'):
            self.forwarded_success = True
        return frame


@contextmanager
def openai_retry_guard():
    """Forward only this invocation's OpenAI inference traffic over loopback."""
    state = RetryGuard()
    prefix = '/' + uuid4().hex + '/v1'

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            route = self.path.removeprefix(prefix)
            if not self.path.startswith(prefix + '/') or route not in ('/responses', '/chat/completions'):
                self.send_error(404)
                return
            length = self.headers.get('Content-Length')
            if not length or not length.isdecimal():
                self.send_error(411)
                return
            body = self.rfile.read(int(length))
            if state.billing_rejected:
                self.send_billing_error()
                return
            upstream = HTTPSConnection('api.openai.com', timeout=60)
            headers = {name: self.headers[name] for name in
                       ('Authorization', 'Content-Type', 'OpenAI-Beta', 'OpenAI-Organization', 'OpenAI-Project')
                       if name in self.headers}
            try:
                upstream.request('POST', '/v1' + route, body, headers)
                response = upstream.getresponse()
                content_type = response.getheader('Content-Type', '')
                if response.status >= 400:
                    body = response.read()
                    if BILLING.search(body.decode('utf-8', 'replace')):
                        state.billing_rejected = True
                        self.send_billing_error()
                    else:
                        self.send_headers(response.status, content_type, response)
                        self.wfile.write(body)
                elif 'text/event-stream' in content_type:
                    self.send_headers(response.status, content_type, response)
                    frame = []
                    while line := response.readline():
                        frame.append(line)
                        if not line.strip():
                            self.wfile.write(state.rewrite_sse(b''.join(frame)))
                            frame.clear()
                            if state.billing_rejected:
                                break
                    if frame:
                        self.wfile.write(state.rewrite_sse(b''.join(frame)))
                else:
                    state.forwarded_success = True
                    self.send_headers(response.status, content_type, response)
                    while chunk := response.read1(65536):
                        self.wfile.write(chunk)
            except OSError:
                # A failed connection after sending a request cannot prove zero use.
                state.forwarded_success = True
            finally:
                upstream.close()

        def send_headers(self, status, content_type, upstream=None):
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            if upstream is not None:
                for name in ('Retry-After', 'Retry-After-Ms', 'X-Request-ID'):
                    if value := upstream.getheader(name):
                        self.send_header(name, value)
            self.end_headers()

        def send_billing_error(self):
            # A 429 alone is retried by OpenCode even with SDK retries disabled.
            self.send_headers(402, 'application/json')
            self.wfile.write(json.dumps({'error': BILLING_ERROR}).encode())

    with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
        thread = Thread(target=server.serve_forever, kwargs={'poll_interval': .05}, daemon=True)
        thread.start()
        try:
            yield f'http://127.0.0.1:{server.server_port}{prefix}', state
        finally:
            server.shutdown()
            thread.join()
