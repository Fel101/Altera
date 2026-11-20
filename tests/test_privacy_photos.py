"""Privacy cleanup must report real photo filesystem failures and allow retry."""
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from core.photos import PhotoService
from core.avatars import AvatarStore


@pytest.fixture
def private_photos(tmp_path):
    avatars = AvatarStore(tmp_path / 'avatars', tmp_path / 'forge')
    photos = PhotoService(tmp_path / 'photos', avatars, None, None, sync=True)
    record = {'id': 'p-0123456789', 'avatar_id': 'synthetic-companion', 'status': 'ready',
              'scene': 'Synthetic photo', 'level': 'off', 'created': '2026-10-26'}
    photos._save(record)
    photos._png(record['id']).write_bytes(b'synthetic-private-photo')
    return NS(photos=photos, avatars=avatars, record=record, pid=record['id'])


@pytest.mark.parametrize('denied_kind', ['png', 'json'])
def test_photo_delete_failure_keeps_record_index_for_retry(private_photos, monkeypatch, denied_kind):
    w = private_photos
    target = w.photos.root / f'{w.pid}.{denied_kind}'
    original = Path.unlink
    def denied(path, *args, **kwargs):
        if path == target:
            raise PermissionError('synthetic denied deletion')
        return original(path, *args, **kwargs)
    with monkeypatch.context() as failure:
        failure.setattr(Path, 'unlink', denied)
        with pytest.raises(PermissionError):
            w.photos.delete_all()
        assert w.photos.get(w.pid)['id'] == w.pid
        assert w.photos._json(w.pid).exists()
    assert w.photos.delete_all() == 1
    assert w.photos.get(w.pid) is None and not list(w.photos.root.iterdir())


def test_privacy_clear_reports_denied_photo_and_can_retry(private_photos, monkeypatch, altera_config):
    from core import companion, memory
    from core.app import create_app
    from core.datastore import get_datastore
    # Load the blueprint before patching the lazy admin dependency; otherwise
    # its from-import can capture this fixture's fake for later test modules.
    from core.routes import core_api  # noqa: F401
    w = private_photos
    altera_config.write_text('voice:\n  tts_enabled: false\nimagegen:\n  enabled: false\n', encoding='utf-8')
    monkeypatch.setattr(companion, 'get_services', lambda: NS(avatars=w.avatars, photos=w.photos))
    monkeypatch.setattr(memory, 'get_memory_manager', lambda: NS(clear_all_memories=lambda: None))
    app = create_app(autostart=False)
    client = app.test_client()
    store = get_datastore()
    sid = store.create_session('Retained until clear succeeds')
    store.save_message(sid, 'user', 'Synthetic private conversation')
    original = Path.unlink
    def denied(path, *args, **kwargs):
        if path == w.photos._png(w.pid):
            raise PermissionError('synthetic denied deletion')
        return original(path, *args, **kwargs)
    with monkeypatch.context() as failure:
        failure.setattr(Path, 'unlink', denied)
        response = client.post('/api/clear-data', json={'confirm': 'delete-all'})
        assert response.status_code == 500 and response.json['code'] == 'clear_incomplete'
        assert w.photos._png(w.pid).exists() and w.photos.get(w.pid)
        assert store.get_message_count(sid) == 1
    assert client.post('/api/clear-data', json={'confirm': 'delete-all'}).status_code == 200
    assert not list(w.photos.root.iterdir()) and store.get_session(sid) is None


def test_photo_clear_includes_orphan_images_and_corrupt_records(private_photos):
    w = private_photos
    (w.photos.root / 'p-1111111111.png').write_bytes(b'orphaned-private-image')
    (w.photos.root / 'p-2222222222.json').write_text('broken metadata containing private content', encoding='utf-8')
    assert w.photos.delete_all() == 3
    assert not list(w.photos.root.iterdir())
