"""Offline socket-level checks for the workspace URL fetcher's network boundary."""
from io import BytesIO
import socket
import ssl

from flask import Flask
import pytest

from core.routes import agent_tools


PUBLIC_IP = '93.184.216.34'


def answer(ip, port):
    return (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', (ip, port or 0))


class FakeSocket:
    def __init__(self, response, attempts, sent):
        self.response, self.attempts, self.sent = response, attempts, sent
        self.closed = False

    def settimeout(self, timeout):
        self.timeout = timeout

    def setsockopt(self, *args):
        pass

    def connect(self, address):
        self.attempts.append(address)

    def sendall(self, data):
        self.sent.append(data)

    def makefile(self, *args):
        return BytesIO(self.response)

    def close(self):
        self.closed = True


@pytest.fixture
def fetch_client(monkeypatch):
    app = Flask(__name__)
    app.register_blueprint(agent_tools.bp)
    monkeypatch.setattr(agent_tools, '_guard', lambda: None)
    return app.test_client()


def fake_network(monkeypatch, resolve, responses=None):
    attempts, sent, sockets = [], [], []
    replies = iter(responses or [b'HTTP/1.1 200 OK\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Length: 5\r\n\r\nHello'])
    def make_socket(*args):
        connection = FakeSocket(next(replies), attempts, sent)
        sockets.append(connection)
        return connection
    monkeypatch.setattr(socket, 'getaddrinfo', resolve)
    monkeypatch.setattr(socket, 'socket', make_socket)
    return attempts, sent, sockets


def test_dns_rebinding_is_rejected_before_connect(fetch_client, monkeypatch):
    calls = []
    def resolve(host, port, *args):
        calls.append((host, port))
        return [answer(PUBLIC_IP if len(calls) == 1 else '127.0.0.1', port)]
    attempts, _, sockets = fake_network(monkeypatch, resolve)
    response = fetch_client.post('/api/tools/fetch', json={'url': 'http://research.example:5001/private'})
    assert response.status_code == 502
    assert response.get_json()['error'] == 'fetch failed'
    assert len(calls) == 2 and attempts == [] and sockets == []


def test_connection_rejects_mixed_public_and_private_dns_answers(fetch_client, monkeypatch):
    calls = []
    def resolve(host, port, *args):
        calls.append(host)
        return [answer(PUBLIC_IP, port)] + ([answer('192.168.1.1', port)] if len(calls) > 1 else [])
    attempts, _, sockets = fake_network(monkeypatch, resolve)
    assert fetch_client.post('/api/tools/fetch', json={'url': 'http://research.example/'}).status_code == 502
    assert attempts == [] and sockets == []


def test_public_fetch_connects_by_ip_and_preserves_host_without_ambient_proxy(fetch_client, monkeypatch):
    calls = []
    def resolve(host, port, *args):
        calls.append((host, port))
        assert host == 'research.example'
        return [answer(PUBLIC_IP, port)]
    monkeypatch.setenv('HTTP_PROXY', 'http://proxy.invalid:8123')
    monkeypatch.setenv('http_proxy', 'http://proxy.invalid:8123')
    monkeypatch.setenv('NO_PROXY', '')
    monkeypatch.setenv('no_proxy', '')
    attempts, sent, _ = fake_network(monkeypatch, resolve)
    response = fetch_client.post('/api/tools/fetch', json={'url': 'http://research.example/article'})
    assert response.status_code == 200 and response.get_json()['content'] == 'Hello'
    assert attempts == [(PUBLIC_IP, 80)] and len(calls) == 2
    assert b'GET /article HTTP/1.1' in b''.join(sent)
    assert b'Host: research.example' in b''.join(sent)


def test_https_keeps_original_sni_and_certificate_verification(fetch_client, monkeypatch):
    monkeypatch.setenv('HTTPS_PROXY', 'http://proxy.invalid:8123')
    monkeypatch.setenv('https_proxy', 'http://proxy.invalid:8123')
    attempts, sent, _ = fake_network(monkeypatch, lambda host, port, *args: [answer(PUBLIC_IP, port)])
    tls = []
    def wrap(context, connection, *, server_hostname, **kwargs):
        tls.append((server_hostname, context.check_hostname, context.verify_mode))
        return connection
    monkeypatch.setattr(ssl.SSLContext, 'wrap_socket', wrap)
    response = fetch_client.post('/api/tools/fetch', json={'url': 'https://research.example/article'})
    assert response.status_code == 200 and response.get_json()['content'] == 'Hello'
    assert attempts == [(PUBLIC_IP, 443)]
    assert tls == [('research.example', True, ssl.CERT_REQUIRED)]
    assert b'Host: research.example' in b''.join(sent)


def test_https_certificate_failure_is_not_bypassed(fetch_client, monkeypatch):
    attempts, _, _ = fake_network(monkeypatch, lambda host, port, *args: [answer(PUBLIC_IP, port)])
    def reject(context, connection, **kwargs):
        raise ssl.SSLCertVerificationError('synthetic untrusted certificate')
    monkeypatch.setattr(ssl.SSLContext, 'wrap_socket', reject)
    response = fetch_client.post('/api/tools/fetch', json={'url': 'https://research.example/'})
    assert response.status_code == 502 and response.get_json()['error'] == 'fetch failed'
    assert attempts == [(PUBLIC_IP, 443)]


def test_redirect_rebinding_is_checked_at_the_next_connection(fetch_client, monkeypatch):
    calls = {}
    def resolve(host, port, *args):
        calls[host] = calls.get(host, 0) + 1
        ip = '127.0.0.1' if host == 'redirect.example' and calls[host] > 1 else PUBLIC_IP
        return [answer(ip, port)]
    first = b'HTTP/1.1 302 Found\r\nLocation: http://redirect.example:5001/private\r\nContent-Length: 0\r\n\r\n'
    attempts, _, _ = fake_network(monkeypatch, resolve, [first])
    response = fetch_client.post('/api/tools/fetch', json={'url': 'http://research.example/article'})
    assert response.status_code == 502
    assert attempts == [(PUBLIC_IP, 80)]
    assert calls == {'research.example': 2, 'redirect.example': 2}


@pytest.mark.parametrize('address', ['100.64.0.1', '100.127.255.254', '127.0.0.1', '10.0.0.1',
                                      '169.254.169.254', '224.0.0.1', '::1', 'fc00::1', 'fe80::1', 'ff02::1'])
def test_shared_and_nonpublic_destinations_are_not_reachable(address):
    assert not agent_tools._public_address(address)


@pytest.mark.parametrize('url', [None, [], 7, 'http://[bad', 'http://research.example:99999'])
def test_malformed_fetch_input_returns_validation_error(fetch_client, url):
    response = fetch_client.post('/api/tools/fetch', json={'url': url})
    assert response.status_code == 400


def test_failed_public_address_can_fall_back_to_another_checked_address(monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *args: [answer(PUBLIC_IP, 80), answer('8.8.8.8', 80)])
    attempts, sent = [], []
    first, second = FakeSocket(b'', attempts, sent), FakeSocket(b'', attempts, sent)
    def failed(address):
        attempts.append(address)
        raise OSError('synthetic connection failure')
    first.connect = failed
    sockets = iter([first, second])
    monkeypatch.setattr(socket, 'socket', lambda *args: next(sockets))
    assert agent_tools._public_connection(('research.example', 80), 3) is second
    assert attempts == [(PUBLIC_IP, 80), ('8.8.8.8', 80)] and first.closed


def test_tools_permission_hot_reloads_without_cached_companion_services(altera_config, monkeypatch, tmp_path):
    from core import companion
    monkeypatch.setattr(agent_tools, 'PROJECT_ROOT', tmp_path)
    def no_services():
        raise AssertionError('Tools permission must not depend on cached companion services')
    monkeypatch.setattr(companion, 'get_services', no_services)
    app = Flask(__name__)
    app.register_blueprint(agent_tools.bp)
    client = app.test_client()
    altera_config.write_text('tools:\n  enabled: true\n', encoding='utf-8')
    assert client.get('/api/tools/files').status_code == 200
    altera_config.write_text('tools:\n  enabled: false\n', encoding='utf-8')
    assert client.get('/api/tools/files').status_code == 403
    altera_config.write_text('tools:\n  enabled: true\n', encoding='utf-8')
    assert client.get('/api/tools/files').status_code == 200


@pytest.mark.parametrize('settings', [{}, {'tools': None}, {'tools': []}, {'tools': 'enabled'},
                                      {'tools': {}}, {'tools': {'enabled': 'true'}}, {'tools': {'enabled': 1}}])
def test_tools_fail_closed_without_an_explicit_boolean_permission(monkeypatch, settings):
    from core import config
    monkeypatch.setattr(config, 'get_config', lambda: settings)
    assert agent_tools._tools_enabled() is False


def test_tools_fail_closed_when_config_cannot_be_read(monkeypatch):
    from core import config
    def broken():
        raise OSError('synthetic unreadable configuration')
    monkeypatch.setattr(config, 'get_config', broken)
    assert agent_tools._tools_enabled() is False
