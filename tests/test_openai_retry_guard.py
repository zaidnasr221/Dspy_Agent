from io import BytesIO
from urllib.parse import urlsplit

import pytest

from facade_dspy import openai_retry_guard as guard


@pytest.mark.parametrize('status,content_type,body,billing', [
    (429, 'application/json', b'{"error":{"message":"You have no credits remaining."}}', True),
    (200, 'text/event-stream', b'data: {"type":"error","code":"insufficient_quota"}\n\n', True),
    (429, 'application/json', b'{"error":{"code":"rate_limit_exceeded"}}', False),
])
def test_only_billing_rejections_block_upstream_retry(monkeypatch,status,content_type,body,billing):
    calls=[]
    servers=[]

    class Upstream:
        def __init__(self, host, timeout):
            assert host=='api.openai.com'

        def request(self, method, path, data, headers):
            calls.append((method,path,data,headers))

        def getresponse(self):
            response=BytesIO(body)
            response.status=status
            headers={'Content-Type':content_type,'Retry-After':'5'}
            response.getheader=lambda name,default=None: headers.get(name,default)
            return response

        def close(self):
            pass

    monkeypatch.setattr(guard,'HTTPSConnection',Upstream)
    server_type=guard.ThreadingHTTPServer

    def capture_server(address,handler):
        server=server_type(address,handler,bind_and_activate=False)
        server.server_port=0
        server.serve_forever=server.shutdown=lambda **kwargs: None
        servers.append(server)
        return server

    class Request:
        def __init__(self,path):
            self.input=BytesIO(
                f'POST {path} HTTP/1.0\r\nContent-Length: 2\r\nAuthorization: Bearer fake\r\n\r\n{{}}'.encode())
            self.output=bytearray()

        def makefile(self,*args):
            return self.input

        def sendall(self,data):
            self.output.extend(data)

    monkeypatch.setattr(guard,'ThreadingHTTPServer',capture_server)
    with guard.openai_retry_guard() as (base,state):
        server=servers[0]
        statuses=(402 if status==429 else 200,402) if billing else (429,429)
        for expected in statuses:
            request=Request(urlsplit(base).path+'/responses')
            server.RequestHandlerClass(request,('127.0.0.1',1),server)
            assert request.output.startswith(f'HTTP/1.0 {expected} '.encode())
            if billing:
                assert b'insufficient_quota' in request.output
            else:
                assert b'rate_limit_exceeded' in request.output
                assert b'Retry-After: 5' in request.output
        assert state.billing_rejected is billing
        assert not state.forwarded_success
    assert len(calls)==(1 if billing else 2)
    assert calls[0][:3]==('POST','/v1/responses',b'{}')
