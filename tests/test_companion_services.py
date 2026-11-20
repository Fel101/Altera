"""Companion reads must not implicitly recover unrelated picture jobs."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading

import pytest

from core import companion


def test_avatar_reads_leave_interrupted_picture_untouched_until_picture_use(tmp_path):
    root = tmp_path / 'data' / 'photos'
    root.mkdir(parents=True)
    path = root / 'p-0123456789.json'
    path.write_text(json.dumps({'id': 'p-0123456789', 'status': 'running'}), 'utf-8')
    original = path.read_bytes()
    services = companion.build_services({}, root=tmp_path)
    assert services.avatars.get('synthetic-mara') is None
    assert services.imagegen_config == {}
    assert path.read_bytes() == original
    assert services.photos.get('p-0123456789')['status'] == 'failed'
    assert json.loads(path.read_text('utf-8'))['status'] == 'failed'


def test_concurrent_first_picture_use_constructs_one_service(tmp_path, monkeypatch):
    created = []
    started, release = threading.Event(), threading.Event()
    service = object()
    def create(*args):
        created.append(args)
        started.set()
        assert release.wait(5)
        return service
    monkeypatch.setattr(companion.photos, 'PhotoService', create)
    services = companion.build_services({}, root=tmp_path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(lambda: services.photos) for _ in range(8)]
        assert started.wait(5)
        release.set()
        assert all(result.result(timeout=5) is service for result in futures)
    assert len(created) == 1


def test_failed_picture_initialization_can_be_explicitly_retried(tmp_path, monkeypatch):
    attempts = []
    service = object()
    def create(*args):
        attempts.append(args)
        if len(attempts) == 1:
            raise OSError('synthetic initialization failure')
        return service
    monkeypatch.setattr(companion.photos, 'PhotoService', create)
    services = companion.build_services({}, root=tmp_path)
    with pytest.raises(OSError):
        _ = services.photos
    assert services.photos is service and len(attempts) == 2
