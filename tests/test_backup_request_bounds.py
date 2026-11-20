"""Backup request parsing must reject ambiguous or oversized JSON before work starts."""
import pytest

pytest_plugins = ('test_backups',)


@pytest.mark.parametrize('path', [
    '/api/backups', '/api/backups/v2', '/api/backups/restore/preview',
    '/api/backups/v2/restore/preview', '/api/backups/restore/apply',
    '/api/backups/v2/restore/apply',
])
@pytest.mark.parametrize('payload', [
    b'{"password":"first","password":"second"}',
    b'{"browser_state":{"drafts":[],"drafts":[]}}',
    b'{"browser_state":{"value":NaN}}',
    b'{"browser_state":{"value":Infinity}}',
    b'{"browser_state":{"value":1e9999}}',
])
def test_ambiguous_json_rejected_before_backup_service(backup_api, monkeypatch, path, payload):
    from core.routes import backups
    monkeypatch.setattr(backups, 'get_backup_service', lambda: pytest.fail('Invalid JSON started backup work'))
    response = backup_api.post(path, data=payload, content_type='application/json')
    assert response.status_code == 400
    assert response.json['code'] == 'backup_error'


@pytest.mark.parametrize('path', ['/api/backups', '/api/backups/restore/preview', '/api/backups/restore/apply'])
def test_json_uses_normal_request_limit_before_backup_service(backup_api, monkeypatch, path):
    from core.routes import backups
    monkeypatch.setattr(backups, 'get_backup_service', lambda: pytest.fail('Oversized JSON started backup work'))
    response = backup_api.post(path, data=b'{"password":"' + b'x' * 600 + b'"}', content_type='application/json')
    assert response.status_code == 413


def test_non_multipart_preview_does_not_receive_archive_upload_limit(backup_api, monkeypatch):
    from core.routes import backups
    monkeypatch.setattr(backups, 'get_backup_service', lambda: pytest.fail('Invalid upload started backup work'))
    response = backup_api.post('/api/backups/restore/preview', data=b'x' * 600, content_type='text/plain')
    assert response.status_code == 413


def test_unknown_length_json_cannot_start_backup_after_exceeding_limit(backup_api, monkeypatch):
    from core.routes import backups
    monkeypatch.setattr(backups, 'get_backup_service', lambda: pytest.fail('Oversized JSON started backup work'))
    response = backup_api.post('/api/backups/restore/preview', data=b'{"backup_id":"' + b'x' * 600 + b'"}',
                               content_type='application/json',
                               environ_overrides={'CONTENT_LENGTH': '', 'wsgi.input_terminated': True})
    assert response.status_code in {400, 413}


def test_unknown_length_valid_prefix_cannot_hide_extra_bytes(backup_api, monkeypatch):
    from core.routes import backups
    monkeypatch.setattr(backups, 'get_backup_service', lambda: pytest.fail('Truncated JSON started backup work'))
    prefix = b'{"format":"zip"}'
    payload = prefix + b' ' * (512 - len(prefix)) + b'extra'
    response = backup_api.post('/api/backups', data=payload, content_type='application/json',
                               environ_overrides={'CONTENT_LENGTH': '', 'wsgi.input_terminated': True})
    assert response.status_code == 413


def test_terminated_stream_cannot_disagree_with_declared_length(backup_api, monkeypatch):
    from core.routes import backups
    monkeypatch.setattr(backups, 'get_backup_service', lambda: pytest.fail('Mismatched JSON started backup work'))
    response = backup_api.post('/api/backups', data=b'{"format":"zip"}', content_type='application/json',
                               environ_overrides={'CONTENT_LENGTH': '0', 'wsgi.input_terminated': True})
    assert response.status_code == 400


def test_terminated_stream_cannot_hide_bytes_after_a_declared_limit(backup_api, monkeypatch):
    from core.routes import backups
    monkeypatch.setattr(backups, 'get_backup_service', lambda: pytest.fail('Truncated JSON started backup work'))
    prefix = b'{"format":"zip"}'
    payload = prefix + b' ' * (512 - len(prefix)) + b'extra'
    response = backup_api.post('/api/backups', data=payload, content_type='application/json',
                               environ_overrides={'CONTENT_LENGTH': '512', 'wsgi.input_terminated': True})
    assert response.status_code == 413


def test_bounded_unknown_length_json_remains_usable(backup_api, monkeypatch):
    from core.routes import backups

    class Service:
        def create(self, browser_state, *, password, recovery_key):
            assert browser_state is None and password is None and recovery_key is None
            return {'id': 'synthetic'}

    monkeypatch.setattr(backups, 'get_backup_service', lambda: Service())
    response = backup_api.post('/api/backups', data=b'{"format":"zip"}', content_type='application/json',
                               environ_overrides={'CONTENT_LENGTH': '', 'wsgi.input_terminated': True})
    assert response.status_code == 201 and response.json['id'] == 'synthetic'
