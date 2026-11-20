"""Synthetic backup-service initialization and cleanup ownership."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import threading
import time

import pytest

from core import backups, datastore, memory

pytest_plugins = ('test_backups',)


def test_concurrent_backup_service_initialization_publishes_only_one(backup_world, monkeypatch):
    store = SimpleNamespace()
    created, mutex = [], threading.Lock()
    start = threading.Barrier(8)

    class Service:
        def __init__(self, root, actual_store, memories, *, on_restored):
            assert actual_store is store and memories is backup_world.memory and callable(on_restored)
            with mutex:
                created.append(self)
            time.sleep(.03)  # Make overlapping first calls observable.

    monkeypatch.setattr(datastore, 'get_datastore', lambda: store)
    monkeypatch.setattr(memory, 'get_memory_manager', lambda: SimpleNamespace(store=backup_world.memory))
    monkeypatch.setattr(backups, 'BackupService', Service)
    def get():
        start.wait(timeout=5)
        return backups.get_backup_service()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: get(), range(8)))
    assert len(created) == 1 and all(result is created[0] for result in results)


def test_failed_backup_service_initialization_is_retryable(backup_world, monkeypatch):
    store, calls = SimpleNamespace(), []

    def create(*args, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            raise OSError('synthetic initialization failure')
        return SimpleNamespace()

    monkeypatch.setattr(datastore, 'get_datastore', lambda: store)
    monkeypatch.setattr(memory, 'get_memory_manager', lambda: SimpleNamespace(store=backup_world.memory))
    monkeypatch.setattr(backups, 'BackupService', create)
    with pytest.raises(OSError, match='synthetic initialization failure'):
        backups.get_backup_service()
    assert not hasattr(store, '_backup_service')
    assert backups.get_backup_service() is store._backup_service and len(calls) == 2
