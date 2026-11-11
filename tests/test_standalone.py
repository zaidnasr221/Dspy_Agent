from socket import gaierror
from urllib.error import URLError

import pytest

from facade_dspy.cli import page_numbers
from facade_dspy.openai_api import OpenAIResponses


def test_page_ranges_preserve_requested_order_and_remove_duplicates():
    assert page_numbers('12,10-13,12') == [12, 10, 11, 13]


def test_responses_retries_dns_but_does_not_repeat_ambiguous_generation(monkeypatch):
    backend = OpenAIResponses.__new__(OpenAIResponses)
    backend.key = 'offline-test'
    calls = []

    def send_after_dns_failure(request):
        calls.append(request.full_url)
        if len(calls) == 1:
            raise URLError(gaierror('DNS lookup failed'))
        return {'status': 'completed'}

    monkeypatch.setattr('facade_dspy.openai_api.sleep', lambda delay: None)
    monkeypatch.setattr(backend, '_send', send_after_dns_failure)
    assert backend.request('', {}) == {'status': 'completed'}
    assert len(calls) == 2

    def ambiguous_failure(request):
        calls.append(request.full_url)
        raise URLError(TimeoutError('Response timed out'))

    monkeypatch.setattr(backend, '_send', ambiguous_failure)
    with pytest.raises(URLError):
        backend.request('', {})
    assert len(calls) == 3
