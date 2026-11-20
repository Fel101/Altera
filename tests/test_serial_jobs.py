"""Owned serial speech work keeps Future semantics and native thread lifetime."""
import threading

import pytest

from core.serial_jobs import OwnedSerialExecutor
from core.state_gate import StateBusy, StateGate


@pytest.fixture
def serial():
    gate = StateGate()
    executor = OwnedSerialExecutor(name='synthetic-serial', gate=gate)
    releases = []
    yield gate, executor, releases
    for event in releases:
        event.set()
    executor.shutdown(wait=True, cancel_futures=True)
    assert gate.join_jobs(timeout=3), 'Disposable serial worker failed to close'


def test_lazy_serial_executor_starts_no_worker_before_work_and_cannot_resume_after_close(serial):
    gate, executor, _ = serial
    assert gate.owned_jobs == 0 and not gate.active
    executor.shutdown(wait=False)
    with pytest.raises(RuntimeError):
        executor.submit(lambda: pytest.fail('Closed executor resumed'))
    assert gate.owned_jobs == 0 and not gate.active


def test_one_worker_preserves_order_and_wait_false_cancels_only_unstarted_work(serial):
    gate, executor, releases = serial
    entered, release = threading.Event(), threading.Event()
    releases.append(release)
    calls = []
    def work(value):
        calls.append(value)
        entered.set()
        assert release.wait(5)
        return value
    first = executor.submit(work, 'first')
    assert entered.wait(3)
    second = executor.submit(work, 'cancelled')
    assert gate.owned_jobs == 1 and not first.done()
    executor.shutdown(wait=False, cancel_futures=True)
    assert second.cancelled() and not first.done() and calls == ['first']
    assert not gate.join_jobs(timeout=0)
    release.set()
    executor.shutdown(wait=True)
    assert first.result() == 'first' and calls == ['first'] and gate.join_jobs(timeout=0)


def test_completed_future_does_not_claim_that_an_open_speech_worker_is_closed(serial):
    gate, executor, _ = serial
    assert executor.submit(lambda value, *, suffix: value + suffix, 'saved', suffix='-clip').result(timeout=3) == 'saved-clip'
    assert gate.owned_jobs == 1
    with pytest.raises(StateBusy):
        with gate.exclusive():
            pytest.fail('Snapshot ignored an open speech worker')
    executor.shutdown(wait=True)
    with gate.exclusive():
        assert gate.maintenance


def test_speech_future_exception_does_not_abandon_later_accepted_work(serial):
    gate, executor, _ = serial
    def fail():
        raise RuntimeError('Synthetic synthesis failed')
    failed = executor.submit(fail)
    after = executor.submit(lambda: 'later-result')
    with pytest.raises(RuntimeError, match='Synthetic synthesis failed'):
        failed.result(timeout=3)
    assert after.result(timeout=3) == 'later-result'
    executor.shutdown(wait=True)
    assert gate.owned_jobs == 0 and not gate.active


def test_failed_native_start_can_be_retried_without_replaying_the_refused_task(serial, monkeypatch):
    gate, executor, _ = serial
    calls = []
    def refuse(*args, **kwargs):
        raise RuntimeError('Synthetic serial worker refusal')
    with monkeypatch.context() as failure:
        failure.setattr(threading.Thread, 'start', refuse)
        with pytest.raises(RuntimeError, match='Synthetic serial worker refusal'):
            executor.submit(calls.append, 'refused')
    assert gate.owned_jobs == 0 and not gate.active
    assert executor.submit(calls.append, 'explicit-retry').result(timeout=3) is None
    executor.shutdown(wait=True)
    assert calls == ['explicit-retry'] and gate.owned_jobs == 0


def test_shutdown_drain_refuses_new_speech_work_and_still_allows_owned_close(serial):
    gate, executor, _ = serial
    assert executor.submit(lambda: 'accepted').result(timeout=3) == 'accepted'
    gate.begin_shutdown()
    with pytest.raises(StateBusy):
        executor.submit(lambda: pytest.fail('Draining admitted another task'))
    executor.shutdown(wait=True)
    assert gate.draining and not gate.active and gate.owned_jobs == 0


def test_nonblocking_close_during_pending_start_preserves_admission_and_cancels_work(serial, monkeypatch):
    gate, executor, releases = serial
    entered, release = threading.Event(), threading.Event()
    releases.append(release)
    start = gate.start_job
    errors, calls = [], []
    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return start(*args, **kwargs)
    monkeypatch.setattr(gate, 'start_job', delayed)
    def submit():
        try:
            executor.submit(calls.append, 'cancelled-before-start')
        except StateBusy:
            errors.append('draining')
    starter = threading.Thread(target=submit)
    starter.start()
    try:
        assert entered.wait(3)
        executor.shutdown(wait=False, cancel_futures=True)
        assert gate.active and gate.owned_jobs == 0 and calls == []
        gate.begin_shutdown()
    finally:
        release.set()
        starter.join(timeout=3)
    assert not starter.is_alive() and errors == ['draining']
    assert gate.owned_jobs == 0 and not gate.active and calls == []
