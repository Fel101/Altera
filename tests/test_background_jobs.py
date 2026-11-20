"""Accepted jobs retain exact thread ownership beyond callback lease release."""
import threading

import pytest

from core.state_gate import StateBusy, StateGate


def test_owned_job_keeps_callback_lease_and_cannot_be_joined_early():
    gate, entered, release = StateGate(), threading.Event(), threading.Event()
    def work():
        entered.set()
        assert release.wait(5)
    worker = gate.start_job(work)
    try:
        assert entered.wait(3) and gate.owned_jobs == 1 and len(gate.active) == 1
        gate.begin_shutdown()
        assert not gate.join_jobs(timeout=.01)
        with pytest.raises(StateBusy):
            gate.start_job(lambda: pytest.fail('Shutdown admitted a new job'))
    finally:
        release.set()
        assert gate.join_jobs(timeout=3)
    assert not worker.is_alive() and not gate.active and gate.owned_jobs == 0


def test_thread_tail_remains_owned_after_callback_releases_its_lease(monkeypatch):
    gate, tail, release = StateGate(), threading.Event(), threading.Event()
    class TailThread(threading.Thread):
        def run(self):
            try:
                super().run()
            finally:
                tail.set()
                assert release.wait(5)
    monkeypatch.setattr(threading, 'Thread', TailThread)
    worker = gate.start_job(lambda: None)
    try:
        assert tail.wait(3) and not gate.active
        assert worker.is_alive() and gate.owned_jobs == 1
        assert not gate.join_jobs(timeout=0)
        with pytest.raises(StateBusy):
            with gate.exclusive():
                pytest.fail('Unclosed thread allowed snapshot maintenance')
    finally:
        release.set()
        assert gate.join_jobs(timeout=3)
    assert gate.owned_jobs == 0
    with gate.exclusive():
        assert gate.maintenance


def test_shutdown_while_thread_start_is_pending_retains_the_accepted_owner(monkeypatch):
    gate, entered, release, ran = StateGate(), threading.Event(), threading.Event(), []
    original_start = threading.Thread.start
    def start(thread):
        if thread.name == 'accepted-synthetic-job':
            entered.set()
            assert release.wait(5)
        return original_start(thread)
    monkeypatch.setattr(threading.Thread, 'start', start)
    starter = threading.Thread(target=lambda: gate.start_job(lambda: ran.append(True), name='accepted-synthetic-job'))
    starter.start()
    try:
        assert entered.wait(3) and gate.owned_jobs == 1 and gate.active
        gate.begin_shutdown()
        assert gate.join_jobs(timeout=0) is False
    finally:
        release.set()
        starter.join(timeout=3)
        assert not starter.is_alive() and gate.join_jobs(timeout=3)
    assert ran == [True] and gate.draining and not gate.active


@pytest.mark.parametrize('phase', ['construct', 'start'])
def test_failure_before_native_thread_start_releases_admission_and_owner(monkeypatch, phase):
    gate = StateGate()
    def refuse(*args, **kwargs):
        raise RuntimeError('Synthetic thread refusal')
    monkeypatch.setattr(threading if phase == 'construct' else threading.Thread,
                        'Thread' if phase == 'construct' else 'start', refuse)
    with pytest.raises(RuntimeError, match='Synthetic thread refusal'):
        gate.start_job(lambda: pytest.fail('Refused job executed'))
    assert not gate.active and gate.owned_jobs == 0 and gate.join_jobs(timeout=0)


def test_callback_exception_releases_lease_but_requires_observed_thread_exit(monkeypatch):
    gate, failures = StateGate(), []
    monkeypatch.setattr(threading, 'excepthook', lambda failure: failures.append(failure.exc_type))
    def fail():
        raise ValueError('Synthetic callback failed')
    worker = gate.start_job(fail)
    assert gate.join_jobs(timeout=3) and not worker.is_alive()
    assert failures == [ValueError] and not gate.active and gate.owned_jobs == 0


def test_owned_callback_cannot_join_its_own_thread():
    gate, observed = StateGate(), []
    worker = gate.start_job(lambda: observed.append(gate.join_jobs(timeout=0)))
    assert gate.join_jobs(timeout=3) and not worker.is_alive()
    assert observed == [False] and not gate.active
