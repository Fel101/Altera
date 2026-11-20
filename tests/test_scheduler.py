"""Whole scheduler cycles own their leases until callbacks actually finish."""
from types import SimpleNamespace
import threading
import time

from flask import Flask, current_app
import pytest

from core.scheduler import OwnedScheduler, start_scheduler
from core.state_gate import StateGate


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(.005)
    assert predicate(), 'Owned scheduler did not reach the expected state'


def fixture(service=None, before=None, interval=.01):
    gate, calls = StateGate(), []
    app = Flask(__name__)
    if service is None:
        service = SimpleNamespace(tick=lambda: calls.append('tick'), run_one=lambda: calls.append('run'))
    logger = SimpleNamespace(exception=lambda *a: calls.append('error'))
    owner = OwnedScheduler(app, name='synthetic-scheduler', label='altera-reflection', service=lambda: service,
                           logger=logger, interval=interval, before_cycle=before, gate=gate)
    return SimpleNamespace(owner=owner, gate=gate, app=app, calls=calls)


def test_constructor_and_idle_wait_do_not_initialize_services_or_hold_a_lease():
    f = fixture(interval=30)
    assert not f.owner.is_alive() and not f.gate.active and not f.calls
    f.owner.start()
    try:
        assert f.owner.is_alive() and not f.calls and not f.gate.active
    finally:
        assert f.owner.close(timeout=2)
    assert f.owner.is_set() and not f.owner.is_alive()
    with pytest.raises(RuntimeError, match='cannot restart'):
        f.owner.start()


def test_stop_keeps_whole_callback_owned_until_it_really_returns():
    entered, release = threading.Event(), threading.Event()
    tail = []
    def run():
        entered.set()
        assert release.wait(5)
        tail.append('published-and-cleaned')
    f = fixture(SimpleNamespace(tick=lambda: None, run_one=run))
    f.owner.start()
    try:
        assert entered.wait(3) and list(f.gate.active.values()) == ['altera-reflection']
        assert f.owner.close(timeout=.01) is False
        assert f.owner.is_alive() and f.gate.active and tail == []
        f.gate.begin_shutdown()
    finally:
        release.set()
        assert f.owner.close(timeout=3)
    assert tail == ['published-and-cleaned'] and not f.gate.active and f.gate.draining


def test_stop_during_planning_does_not_start_generation():
    entered, release = threading.Event(), threading.Event()
    generated = []
    def tick():
        entered.set()
        assert release.wait(5)
    f = fixture(SimpleNamespace(tick=tick, run_one=lambda: generated.append(True)))
    f.owner.start()
    try:
        assert entered.wait(3)
        f.owner.set()
        assert f.owner.close(timeout=.01) is False and f.gate.active
    finally:
        release.set()
        assert f.owner.close(timeout=3)
    assert generated == [] and not f.gate.active


def test_cycle_guard_precedes_initialization_and_generation_and_has_application_context():
    seen = []
    f = fixture(before=lambda: seen.append('guard'))
    f.owner._service = lambda: (seen.append('initialize') or SimpleNamespace(
        tick=lambda: seen.append(('tick', current_app.name)), run_one=lambda: (seen.append('run'), f.owner.set())))
    f.owner.start()
    try:
        wait_for(lambda: not f.owner.is_alive())
    finally:
        assert f.owner.close(timeout=3)
    assert seen == ['guard', 'initialize', ('tick', f.app.name), 'guard', 'run']
    assert not f.gate.active


def test_private_guard_revocation_stops_without_initialization_and_releases_admission():
    f = fixture()
    def revoke():
        f.gate.begin_shutdown()
        f.owner.set()
        raise RuntimeError('Synthetic context no longer authorized')
    f.owner._before = revoke
    f.owner.start()
    try:
        wait_for(lambda: not f.owner.is_alive())
    finally:
        assert f.owner.close(timeout=3)
    assert f.calls == ['error'] and not f.gate.active and f.gate.draining


def test_maintenance_defers_cycles_and_drain_prevents_new_work():
    f = fixture()
    f.gate.maintenance = True
    f.owner.start()
    try:
        time.sleep(.04)
        assert not f.calls and not f.gate.active
        f.gate.maintenance = False
        wait_for(lambda: 'run' in f.calls)
        f.gate.begin_shutdown()
        wait_for(lambda: not f.owner.is_alive())
        assert not f.gate.active
    finally:
        assert f.owner.close(timeout=3)


def test_registration_precedes_execution_and_existing_controller_is_not_duplicated():
    f, registered = fixture(interval=30), []
    owner = start_scheduler(f.app, key='synthetic', name='synthetic-owner', label='altera-presence',
                            service=lambda: pytest.fail('Unexpected early cycle'), logger=SimpleNamespace(exception=lambda *a: None),
                            gate=f.gate, interval=30, register=registered.append)
    try:
        assert registered == [owner] and owner.is_alive()
        assert start_scheduler(f.app, key='synthetic') is owner
    finally:
        assert owner.close(timeout=3)


def test_thread_start_failure_keeps_stopped_owner_reviewable(monkeypatch):
    f = fixture()
    monkeypatch.setattr(threading.Thread, 'start', lambda self: (_ for _ in ()).throw(RuntimeError('Synthetic thread refusal')))
    with pytest.raises(RuntimeError, match='thread refusal'):
        f.owner.start()
    assert not f.owner.is_alive() and f.owner.is_set() and f.owner.close(timeout=0)
    assert not f.gate.active and not f.calls
