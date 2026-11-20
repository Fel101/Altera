"""The real photo queue serializes work and retains its owned native worker."""
import threading

import pytest

from core import state_gate
from core.avatars import AvatarStore
from core.photos import PhotoService


@pytest.fixture
def photo_worker(tmp_path, monkeypatch):
    gate = state_gate.StateGate()
    monkeypatch.setattr(state_gate, 'STATE', gate)
    avatars = AvatarStore(tmp_path / 'avatars', tmp_path / 'forge')
    service = PhotoService(tmp_path / 'photos', avatars, None, None)
    releases = []
    yield gate, service, releases
    for event in releases:
        event.set()
    assert gate.join_jobs(timeout=3), 'Disposable photo worker failed to close'


def test_photo_queue_is_inert_and_restarts_without_an_idle_worker(photo_worker, monkeypatch):
    gate, service, _ = photo_worker
    saved = []
    monkeypatch.setattr(service, '_process', saved.append)
    assert service._thread is None and gate.owned_jobs == 0 and not gate.active
    service._enqueue('first')
    first = service._thread
    assert gate.join_jobs(timeout=3)
    assert saved == ['first'] and service._thread is None and not first.is_alive()
    service._enqueue('second')
    assert gate.join_jobs(timeout=3)
    assert saved == ['first', 'second'] and service._thread is None
    assert gate.owned_jobs == 0 and not gate.active and service._queue.unfinished_tasks == 0


def test_accepted_pictures_finish_serially_while_drain_refuses_new_work(photo_worker, monkeypatch, tmp_path):
    gate, service, releases = photo_worker
    entered, release = threading.Event(), threading.Event()
    releases.append(release)
    saved = []
    def process(pid):
        if pid == 'first':
            entered.set()
            assert release.wait(5)
        (tmp_path / pid).write_text('synthetic saved picture', 'ascii')
        saved.append(pid)
    monkeypatch.setattr(service, '_process', process)
    service._enqueue('first')
    assert entered.wait(3)
    worker = service._thread
    service._enqueue('second')
    assert gate.owned_jobs == 1 and service._thread is worker
    with pytest.raises(state_gate.StateBusy):
        with gate.exclusive():
            pytest.fail('Snapshot started over queued picture work')
    gate.begin_shutdown()
    with pytest.raises(state_gate.StateBusy):
        service._enqueue('refused')
    assert not gate.join_jobs(timeout=0) and saved == []
    release.set()
    assert gate.join_jobs(timeout=3)
    assert saved == ['first', 'second'] and not (tmp_path / 'refused').exists()
    assert not gate.active and gate.draining and service._queue.unfinished_tasks == 0


def test_failed_photo_thread_start_does_not_leave_a_phantom_worker_or_queued_lease(photo_worker, monkeypatch):
    gate, service, _ = photo_worker
    saved = []
    monkeypatch.setattr(service, '_process', saved.append)
    def refuse(*args, **kwargs):
        raise RuntimeError('Synthetic photo worker start refusal')
    with monkeypatch.context() as failure:
        failure.setattr(threading.Thread, 'start', refuse)
        with pytest.raises(RuntimeError, match='Synthetic photo worker start refusal'):
            service._enqueue('refused')
    assert service._thread is None and service._queue.empty() and not gate.active and gate.owned_jobs == 0
    service._enqueue('reviewed-retry')
    assert gate.join_jobs(timeout=3) and saved == ['reviewed-retry']


def test_a_photo_process_exception_does_not_abandon_the_next_accepted_item(photo_worker, monkeypatch):
    gate, service, releases = photo_worker
    entered, release = threading.Event(), threading.Event()
    releases.append(release)
    saved = []
    def process(pid):
        if pid == 'failed':
            entered.set()
            assert release.wait(5)
            raise RuntimeError('Synthetic picture processing failure')
        saved.append(pid)
    monkeypatch.setattr(service, '_process', process)
    service._enqueue('failed')
    assert entered.wait(3)
    service._enqueue('next')
    release.set()
    assert gate.join_jobs(timeout=3)
    assert saved == ['next'] and not gate.active and service._queue.unfinished_tasks == 0


def test_photo_thread_local_cleanup_is_owned_after_the_queue_is_empty(photo_worker, monkeypatch):
    gate, service, releases = photo_worker
    entered, release, local = threading.Event(), threading.Event(), threading.local()
    releases.append(release)
    class PrivateTail:
        def __del__(self):
            entered.set()
            release.wait(5)
    monkeypatch.setattr(service, '_process', lambda pid: setattr(local, 'value', PrivateTail()))
    service._enqueue('synthetic')
    assert entered.wait(3)
    assert service._thread is None and service._queue.empty() and not gate.active
    assert gate.owned_jobs == 1 and not gate.join_jobs(timeout=0)
    with pytest.raises(state_gate.StateBusy):
        with gate.exclusive():
            pytest.fail('Snapshot started before photo thread-local cleanup finished')
    release.set()
    assert gate.join_jobs(timeout=3)
