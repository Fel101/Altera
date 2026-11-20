"""ComfyUI transport tests use disposable HTTP fixtures, never a model service."""
import json
from types import SimpleNamespace
import urllib.error

import pytest

from core import imagegen

pytest_plugins = ('test_voice_http',)


def test_requests_ignore_proxy_and_global_opener(recipients, monkeypatch):
    sink = recipients()
    target = recipients(body=b'{"prompt_id":"job-1"}')
    for key in ('HTTP_PROXY', 'http_proxy', 'HTTPS_PROXY', 'https_proxy', 'ALL_PROXY', 'all_proxy'):
        monkeypatch.setenv(key, sink.url)
    for key in ('NO_PROXY', 'no_proxy'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(imagegen.urllib.request, '_opener', SimpleNamespace(
        open=lambda *a, **kw: pytest.fail('Global opener received a ComfyUI request.')))
    comfy = imagegen.Comfy(target.url + '/comfy')
    assert comfy.submit({'synthetic': 'scene'}) == 'job-1'
    assert target.received[0][:2] == ('POST', '/comfy/prompt')
    assert json.loads(target.received[0][2])['prompt'] == {'synthetic': 'scene'}
    assert not sink.received


@pytest.mark.parametrize('status', [301, 302, 303, 307, 308])
@pytest.mark.parametrize('method', ['GET', 'POST'])
def test_redirect_does_not_forward_private_payload(recipients, status, method):
    sink = recipients()
    target = recipients(status=status, location=sink.url + '/other', body=b'private details')
    comfy = imagegen.Comfy(target.url)
    with pytest.raises(imagegen.ComfyTransportError, match=f'HTTP {status}') as caught:
        comfy._request(method, '/prompt', data=b'private scene' if method == 'POST' else None)
    assert len(target.received) == 1 and not sink.received
    assert 'private' not in str(caught.value) and sink.url not in str(caught.value)


@pytest.mark.parametrize('framing', [{}, {'chunked': True}, {'length': False}])
@pytest.mark.parametrize('path,constant', [('/system_stats', 'MAX_JSON_BYTES'), ('/view?filename=x', 'MAX_IMAGE_BYTES')])
def test_response_limits_cover_metadata_and_images(recipients, monkeypatch, framing, path, constant):
    monkeypatch.setattr(imagegen, constant, 32)
    target = recipients(body=b'x' * 33, **framing)
    with pytest.raises(imagegen.ComfyTransportError, match='oversized'):
        imagegen.Comfy(target.url)._request('GET', path)


def test_truncated_reply_and_malformed_json_fail_as_transport_errors(recipients):
    truncated = recipients(body=b'{}', declared=10)
    with pytest.raises(imagegen.ComfyTransportError, match='invalid'):
        imagegen.Comfy(truncated.url).get_json('/system_stats')
    malformed = recipients(body=b'not json')
    with pytest.raises(imagegen.ComfyTransportError, match='JSON'):
        imagegen.Comfy(malformed.url).get_json('/system_stats')
    wrong_shape = recipients(body=b'[]')
    with pytest.raises(imagegen.ComfyTransportError, match='metadata'):
        imagegen.Comfy(wrong_shape.url).get_json('/system_stats')


def test_reference_upload_requests_no_overwrite_and_checks_server_identity(monkeypatch):
    comfy = imagegen.Comfy()
    calls = []
    name = 'altera_' + 'a' * 32 + '_0.png'

    def request(method, path, data, headers, timeout):
        calls.append((method, path, data))
        return json.dumps({'name': name, 'subfolder': 'altera', 'type': 'temp'}).encode()

    monkeypatch.setattr(comfy, '_request', request)
    assert comfy.upload_image(b'synthetic reference', name) == f'altera/{name} [temp]'
    assert calls[0][:2] == ('POST', '/upload/image')
    assert b'name="overwrite"\r\n\r\nfalse\r\n' in calls[0][2]


@pytest.mark.parametrize('changes', [
    {'name': 'other.png'}, {'name': 'altera_file (1).png'}, {'subfolder': '../other'},
    {'subfolder': ''}, {'type': 'input'}, {'type': None},
])
def test_reference_upload_refuses_changed_server_identity(monkeypatch, changes):
    comfy = imagegen.Comfy()
    metadata = {'name': 'altera_file.png', 'subfolder': 'altera', 'type': 'temp', **changes}
    monkeypatch.setattr(comfy, '_request', lambda *args, **kwargs: json.dumps(metadata).encode())
    with pytest.raises(imagegen.ComfyTransportError, match='image metadata'):
        comfy.upload_image(b'synthetic reference', 'altera_file.png')


@pytest.mark.parametrize('stats', [{}, {'devices': []},
    {'devices': [{'vram_total': 100, 'vram_free': 101}], 'system': {'ram_free': 100}},
    {'devices': [{'vram_total': 100, 'vram_free': 1}], 'system': {'ram_free': float('nan')}}])
def test_bad_statistics_remain_transport_errors(stats):
    with pytest.raises(imagegen.ComfyTransportError, match='statistics'):
        imagegen._stats(SimpleNamespace(get_json=lambda *a, **kw: stats))


@pytest.mark.parametrize('base', ['file:///tmp/model', 'http://user:secret@localhost',
    'http://localhost:0', 'http://localhost:bad', 'http://localhost:70000',
    'http://localhost?token=secret', 'http://localhost#secret', 'http://localhost\\other',
    'http://localhost/\nother', None])
def test_invalid_url_never_opens(base, monkeypatch):
    monkeypatch.setattr(imagegen.urllib.request, 'build_opener', lambda *a: pytest.fail('Unexpected transport.'))
    with pytest.raises(imagegen.ComfyTransportError, match='URL'):
        imagegen.Comfy(base)._request('GET', '/system_stats')


@pytest.mark.parametrize('job_id', ['../free', 'x/y', '?x', '', '.', '..', 'x' * 129, None])
def test_untrusted_job_id_cannot_change_history_request(job_id, monkeypatch):
    comfy = imagegen.Comfy()
    monkeypatch.setattr(comfy, 'get_json', lambda *a, **kw: pytest.fail('Unexpected request.'))
    with pytest.raises(imagegen.ComfyTransportError, match='identifier'):
        comfy.wait(job_id)


def test_provider_error_body_is_unread_and_closed(monkeypatch):
    class Body:
        closed = False

        def read(self, *args):
            pytest.fail('Provider error body was read.')

        def close(self):
            self.closed = True

    body = Body()

    def refuse(*args, **kwargs):
        raise urllib.error.HTTPError('http://private.invalid/secret', 500, 'private', {}, body)

    monkeypatch.setattr(imagegen.urllib.request, 'build_opener', lambda *a: SimpleNamespace(open=refuse))
    with pytest.raises(imagegen.ComfyTransportError, match='HTTP 500') as caught:
        imagegen.Comfy()._request('GET', '/system_stats')
    assert body.closed and 'private' not in str(caught.value)


def test_transport_error_never_starts_fallback_but_workflow_rejection_can():
    calls = []

    class Backend:
        name = 'fake'

        def __init__(self, error=None):
            self.error = error

        def render(self, *args):
            calls.append(self.error)
            if self.error:
                raise self.error
            return b'picture'

    fallback = Backend()
    guarded = imagegen.FallbackBackend(Backend(imagegen.ComfyTransportError('invalid')), fallback)
    with pytest.raises(imagegen.ComfyTransportError):
        guarded.render([], 'scene', '', 1, (1, 1), 1)
    assert len(calls) == 1
    calls.clear()
    allowed = imagegen.FallbackBackend(Backend(imagegen.ImageGenError('workflow rejected')), fallback)
    assert allowed.render([], 'scene', '', 1, (1, 1), 1) == b'picture'
    assert len(calls) == 2


def test_guarded_monitor_preserves_transport_failure_through_cleanup():
    calls = []

    class Comfy:
        def submit(self, graph):
            calls.append('submit')
            return 'job-1'

        def get_json(self, path, timeout=30):
            calls.append(path)
            if path == '/system_stats':
                raise imagegen.ComfyTransportError('invalid transport')
            return {'queue_running': [], 'queue_pending': []}

        def _request(self, method, path, **kwargs):
            calls.append(path)
            return b'{}'

    class Primary:
        name = 'primary'

        def render(self, *args):
            return imagegen.run_guarded(Comfy(), {}, sleep=lambda _: None, clock=lambda: 0)

    class Fallback:
        name = 'fallback'

        def render(self, *args):
            pytest.fail('A transport failure must not start a second generation.')

    with pytest.raises(imagegen.ComfyTransportError, match='invalid transport'):
        imagegen.FallbackBackend(Primary(), Fallback()).render([], '', '', 0, (1, 1), 1)
    assert calls == ['submit', '/system_stats', '/interrupt', '/free', '/queue']


def test_workflow_http_400_is_distinct_from_other_provider_errors(recipients):
    rejected = recipients(status=400, body=b'private provider details')
    with pytest.raises(imagegen.ImageGenError, match='HTTP 400') as caught:
        imagegen.Comfy(rejected.url).submit({})
    assert not isinstance(caught.value, imagegen.ComfyTransportError)
    assert 'private' not in str(caught.value)
    failed = recipients(status=500)
    with pytest.raises(imagegen.ComfyTransportError, match='HTTP 500'):
        imagegen.Comfy(failed.url).submit({})
