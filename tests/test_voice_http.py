"""Synthetic loopback recipients only; never starts an installed speech engine."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from core import tts, voice_http, voice_service
from test_blueprints import create_app


@pytest.fixture
def recipients():
    servers = []

    def start(*, body=b'{}', status=200, location=None, chunked=False, length=True, declared=None):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def answer(self):
                data = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                received.append((self.command, self.path, data))
                self.send_response(status)
                if location:
                    self.send_header('Location', location)
                if chunked:
                    self.send_header('Transfer-Encoding', 'chunked')
                elif length:
                    self.send_header('Content-Length', str(len(body) if declared is None else declared))
                self.end_headers()
                try:
                    if chunked:
                        self.wfile.write(f'{len(body):x}\r\n'.encode() + body + b'\r\n0\r\n\r\n')
                    else:
                        self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # Refusal can close a bounded read before a synthetic sender finishes.

            do_GET = do_POST = answer

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01))
        thread.start()
        servers.append((server, thread))
        return SimpleNamespace(url=f'http://127.0.0.1:{server.server_port}', received=received)

    yield start
    for server, thread in reversed(servers):
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive()


def test_metadata_and_speech_ignore_ambient_proxy_and_global_opener(recipients, monkeypatch):
    sink = recipients()
    target = recipients(body=b'{"loaded":["kokoro"]}')
    for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
        monkeypatch.setenv(key, sink.url)
    for key in ('NO_PROXY', 'no_proxy'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(voice_http.urllib.request, '_opener', SimpleNamespace(
        open=lambda *a, **kw: pytest.fail('Ambient urllib opener must not receive voice requests.')))
    cfg = tts.settings({'voice': {'tts_engine': 'kokoro', 'kokoro': {'url': target.url + '/speech'}}})
    assert voice_service.info(cfg) == {'loaded': ['kokoro']}
    assert tts._kokoro_wav('Synthetic voice text.', cfg) == b'{"loaded":["kokoro"]}'
    assert [row[:2] for row in target.received] == [('GET', '/speech/api/info'), ('POST', '/speech/api/speak')]
    assert json.loads(target.received[1][2])['text'] == 'Synthetic voice text.'
    assert not sink.received


@pytest.mark.parametrize('status', [301, 302, 303, 307, 308])
@pytest.mark.parametrize('endpoint,data', [('/api/info', None), ('/api/speak', b'{"text":"private synthetic"}')])
def test_redirect_never_reaches_another_recipient(recipients, status, endpoint, data):
    sink = recipients()
    redirect = recipients(status=status, location=sink.url + '/unexpected', body=b'private error body')
    with pytest.raises(voice_http.VoiceHTTPError) as caught:
        voice_http.read(redirect.url, endpoint, data=data, timeout=2, limit=2048)
    assert caught.value.status == status
    assert sink.url not in str(caught.value) and 'private' not in str(caught.value)
    assert len(redirect.received) == 1 and not sink.received


@pytest.mark.parametrize('framing', [{'chunked': True}, {'length': False}, {}])
def test_both_declared_and_streamed_oversize_are_refused(recipients, framing):
    target = recipients(body=b'x' * 2049, **framing)
    with pytest.raises(voice_http.VoiceHTTPError, match='size limit'):
        voice_http.read(target.url, '/api/info', timeout=2, limit=2048)


@pytest.mark.parametrize('base', ['file:///tmp/voice', 'ftp://example.invalid', 'http://',
    'http://user:password@example.invalid', 'http://example.invalid?key=secret',
    'http://example.invalid#secret', 'http://example.invalid:bad', 'http://example.invalid:0',
    'http://example.invalid:70000', 'http://example.invalid/\nsecret', 'http://example.invalid\\secret',
    'http://example.invalid?', 'http://example.invalid#'])
def test_invalid_endpoint_is_refused_before_transport(base, monkeypatch):
    monkeypatch.setattr(voice_http.urllib.request, 'build_opener', lambda *a: pytest.fail('No transport for invalid URL.'))
    with pytest.raises(voice_http.VoiceHTTPError, match='URL'):
        voice_http.read(base, '/api/info', timeout=2, limit=2048)


@pytest.mark.parametrize('length', ['-1', 'not-a-size', '2049'])
def test_invalid_or_excessive_declared_length_closes_without_reading(length, monkeypatch):
    response = SimpleNamespace(status=200, headers={'Content-Length': length})
    closed = []

    class Context:
        def __enter__(self):
            return response

        def __exit__(self, *args):
            closed.append(True)

    response.read = lambda *a: pytest.fail('Invalid length must not read provider body.')
    monkeypatch.setattr(voice_http.urllib.request, 'build_opener',
        lambda *a: SimpleNamespace(open=lambda *a, **kw: Context()))
    with pytest.raises(voice_http.VoiceHTTPError):
        voice_http.read('http://127.0.0.1:1', '/api/info', timeout=2, limit=2048)
    assert closed == [True]


def test_provider_error_is_fixed_and_does_not_read_or_expose_its_body(monkeypatch):
    class Unreadable:
        closed = False

        def read(self, *args):
            pytest.fail('Provider error body must not be read.')

        def close(self):
            self.closed = True

    body = Unreadable()

    def refuse(*a, **kw):
        raise voice_http.urllib.error.HTTPError('http://private.invalid/secret', 500, 'private model path', {}, body)

    monkeypatch.setattr(voice_http.urllib.request, 'build_opener', lambda *a: SimpleNamespace(open=refuse))
    cfg = tts.settings({'voice': {'tts_engine': 'kokoro'}})
    with pytest.raises(tts.TTSError, match='HTTP 500') as caught:
        tts._kokoro_wav('Synthetic.', cfg)
    assert not isinstance(caught.value, tts.TTSUnavailable)
    assert 'private' not in str(caught.value) and body.closed


def test_oversized_audio_is_not_published_or_sent_to_cloud(recipients, monkeypatch, tmp_path):
    target = recipients(body=b'x' * 257)
    cfg = tts.settings({'voice': {'tts_engine': 'kokoro', 'cloud_fallback': True,
        'kokoro': {'url': target.url, 'autostart': False}}})
    monkeypatch.setattr(tts, 'settings', lambda: cfg)
    cache = tmp_path / 'audio'
    monkeypatch.setattr(tts, 'AUDIO_CACHE_DIR', str(cache))
    monkeypatch.setattr(tts, 'MAX_WAV_BYTES', 256)
    monkeypatch.setattr(tts, '_service_ready', True)
    monkeypatch.setattr(tts, '_down_until', 0)
    monkeypatch.setattr(tts, 'generate_speech', lambda *a, **kw: pytest.fail('Invalid output is not a cloud fallback.'))
    with pytest.raises(tts.TTSError, match='size limit'):
        tts.synthesize_long('Synthetic oversized clip.', allow_cloud=True)
    assert not list(cache.iterdir()) and len(target.received) == 1


@pytest.mark.parametrize('method', ['GET', 'HEAD'])
def test_voice_status_route_cannot_start_a_service(method, monkeypatch):
    from core.routes import engagement
    cfg = {'voice': {'tts_enabled': True, 'tts_engine': 'kokoro', 'kokoro': {'autostart': True}}}
    tts_cfg = tts.settings(cfg)
    monkeypatch.setattr(engagement, 'get_config', lambda: cfg)
    monkeypatch.setattr(tts, 'settings', lambda: tts_cfg)
    monkeypatch.setattr(voice_service, 'info', lambda *a, **kw: None)
    monkeypatch.setattr(voice_service, '_owner', None)
    monkeypatch.setattr(voice_service, '_proc', None)
    monkeypatch.setattr(voice_service, '_closing', False)
    monkeypatch.setattr(voice_service, 'ensure_started_async', lambda *a: pytest.fail('Status must not queue startup.'))
    monkeypatch.setattr('core.profile.load_user_profile', lambda: {'preferences': {'voice_enabled': True}})
    response = create_app('engagement').test_client().open('/api/voice/status', method=method)
    assert response.status_code == 200
    if method == 'GET':
        assert response.get_json()['voice_state'] == 'down'


def test_non_object_or_invalid_json_metadata_are_unavailable(recipients):
    for body in (b'invalid-json', b'[]', b'"scalar"'):
        target = recipients(body=body)
        cfg = tts.settings({'voice': {'tts_engine': 'kokoro', 'kokoro': {'url': target.url}}})
        assert voice_service.info(cfg) is None


@pytest.mark.parametrize('url,location', [('http://127.0.0.1:8770', 'local'),
    ('https://voice.example.invalid/speech', 'configured server')])
def test_voice_label_does_not_describe_an_explicit_remote_endpoint_as_local(url, location):
    cfg = tts.settings({'voice': {'tts_engine': 'kokoro', 'kokoro': {'url': url}}})
    assert tts.voice_label(cfg) == f'Kokoro af_heart ({location})'


def test_incomplete_response_closes_and_is_not_a_cloud_fallback(recipients):
    # The declared length arrives from the peer, rather than trusted configuration.
    target = recipients(body=b'x', declared=5)
    cfg = tts.settings({'voice': {'tts_engine': 'kokoro', 'kokoro': {'url': target.url}}})
    with pytest.raises(tts.TTSError, match='invalid response') as caught:
        tts._kokoro_wav('Synthetic incomplete response.', cfg)
    assert not isinstance(caught.value, tts.TTSUnavailable)
