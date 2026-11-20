"""Exercise Forge transport against disposable loopback-only HTTP fixtures."""
import json
from types import SimpleNamespace
import urllib.error

import pytest

from core import forge

pytest_plugins = ('test_voice_http',)


def client_for(url):
    return forge.ForgeClient({'companion': {'forge': {'url': url}}})


def test_direct_request_ignores_proxy_and_global_opener(recipients, monkeypatch):
    sink = recipients()
    target = recipients(body=b'{"api_version": 4}')
    for key in ('HTTP_PROXY', 'http_proxy', 'HTTPS_PROXY', 'https_proxy', 'ALL_PROXY', 'all_proxy'):
        monkeypatch.setenv(key, sink.url)
    for key in ('NO_PROXY', 'no_proxy'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(forge.urllib.request, '_opener', SimpleNamespace(
        open=lambda *a, **kw: pytest.fail('Global opener received a Forge request.')))
    assert client_for(target.url + '/forge')._call('GET', '/api/config') == {'api_version': 4}
    assert target.received[0][:2] == ('GET', '/forge/api/config')
    assert not sink.received


@pytest.mark.parametrize('status', [301, 302, 303, 307, 308])
@pytest.mark.parametrize('method', ['GET', 'POST'])
def test_redirect_cannot_select_another_recipient(recipients, status, method):
    sink = recipients()
    target = recipients(status=status, location=sink.url + '/other', body=b'private details')
    with pytest.raises(forge.ForgeError, match=f'HTTP {status}') as error:
        client_for(target.url)._call(method, '/api/generate', {'synthetic': 'private'} if method == 'POST' else None)
    assert len(target.received) == 1 and not sink.received
    assert 'private' not in str(error.value) and sink.url not in str(error.value)


@pytest.mark.parametrize('framing', [{}, {'chunked': True}, {'length': False}])
def test_response_size_is_bounded_for_each_framing(recipients, monkeypatch, framing):
    monkeypatch.setattr(forge, 'MAX_RESPONSE_BYTES', 32)
    target = recipients(body=b'x' * 33, **framing)
    with pytest.raises(forge.ForgeError, match='oversized'):
        client_for(target.url)._call('GET', '/api/config')


def test_truncated_response_is_rejected(recipients):
    target = recipients(body=b'{}', declared=9)
    with pytest.raises(forge.ForgeError, match='invalid'):
        client_for(target.url)._call('GET', '/api/config')


@pytest.mark.parametrize('url', ['file://localhost/x', 'http://user:secret@localhost:8765',
    'http://localhost:0', 'http://localhost:bad', 'http://localhost:70000',
    'http://localhost?token=secret', 'http://localhost#secret', 'http://localhost\\other',
    'http://localhost/\nother', 'http://example.com'])
def test_invalid_or_nonlocal_url_never_opens(url, monkeypatch):
    monkeypatch.setattr(forge.urllib.request, 'build_opener', lambda *a: pytest.fail('Unexpected transport.'))
    with pytest.raises(forge.ForgeError, match='this computer'):
        client_for(url)._call('GET', '/api/config')


def test_error_body_is_not_read_and_is_closed(monkeypatch):
    class Body:
        closed = False

        def read(self, *args):
            pytest.fail('Error body was read.')

        def close(self):
            self.closed = True

    body = Body()

    def refuse(*args, **kwargs):
        raise urllib.error.HTTPError('http://private.invalid/secret', 500, 'private', {}, body)

    with pytest.raises(forge.ForgeError, match='HTTP 500') as error:
        forge.ForgeClient({}, opener=refuse)._call('GET', '/api/config')
    assert body.closed and 'private' not in str(error.value)


@pytest.mark.parametrize('job_id', ['../generate', 'x/y', '?x', '', '.', '..', 'x' * 129, None])
def test_cancel_rejects_non_identifiers_before_request(job_id):
    client = forge.ForgeClient({}, opener=lambda *a, **kw: pytest.fail('Unexpected request.'))
    client.cancel(job_id)


def test_cancel_posts_to_exact_job(recipients):
    target = recipients()
    client_for(target.url).cancel('job-42_a')
    assert target.received[0][:2] == ('POST', '/api/jobs/job-42_a/cancel')
    assert json.loads(target.received[0][2]) == {}
