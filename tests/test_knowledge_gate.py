"""Knowledge maintenance excludes all foreign work, including another host's HTTP work."""
import threading

from flask import Flask, g
import pytest

from core import state_gate
from core.datastore import DataStore
from core.knowledge import KnowledgeError, KnowledgeService
from core.turns import get_turn_service


@pytest.fixture
def knowledge_gate(tmp_path, monkeypatch):
    gate = state_gate.StateGate()
    monkeypatch.setattr(state_gate, 'STATE', gate)
    store = DataStore(str(tmp_path / 'isolated.db'))
    get_turn_service(store)
    return gate, KnowledgeService(store, None), Flask(__name__)


def test_knowledge_excludes_only_its_current_request_and_never_releases_that_owner(knowledge_gate):
    gate, service, app = knowledge_gate
    outer = gate.enter('other-host-http')
    try:
        with app.test_request_context('/'):
            g.altera_state_lease = outer
            with service.changing(''):
                assert gate.maintenance and gate.active == {outer.key: 'other-host-http'}
                with pytest.raises(state_gate.StateBusy):
                    gate.enter('new-writer')
        assert not gate.maintenance and gate.active == {outer.key: 'other-host-http'}
    finally:
        outer.release()


@pytest.mark.parametrize('label', ['other-host-http', 'request:knowledge.read', 'altera-notes', 'altera-voice-note', 'unrelated-writer',
                                  'altera-presence-cycle', 'altera-reflection-cycle'])
def test_knowledge_refuses_other_request_or_background_lease(knowledge_gate, label):
    gate, service, app = knowledge_gate
    outer, other = gate.enter('other-host-http'), gate.enter(label)
    try:
        with app.test_request_context('/'):
            g.altera_state_lease = outer
            with pytest.raises(KnowledgeError, match='another request or background'):
                with service.changing(''):
                    pytest.fail('Foreign work must prevent mutation')
        assert not gate.maintenance and set(gate.active) == {outer.key, other.key}
    finally:
        outer.release()
        other.release()


@pytest.mark.parametrize('invalid', ['released', 'foreign', 'object'])
def test_knowledge_cannot_borrow_unverified_request_identity(knowledge_gate, invalid):
    gate, service, app = knowledge_gate
    other = gate.enter('other-host-http')
    released = gate.enter('other-host-http')
    released.release()
    foreign = state_gate.StateGate().enter('other-host-http')
    try:
        with app.test_request_context('/'):
            g.altera_state_lease = {'released': released, 'foreign': foreign, 'object': object()}[invalid]
            with pytest.raises(KnowledgeError):
                with service.changing(''):
                    pytest.fail('An invalid identity cannot exempt live work')
        assert gate.active == {other.key: 'other-host-http'} and not gate.maintenance
    finally:
        other.release()
        foreign.release()


@pytest.mark.parametrize('fence', ['draining', 'maintenance', 'fault'])
def test_known_fence_refuses_knowledge_before_lazy_ledger_initialization(knowledge_gate, monkeypatch, fence):
    gate, service, _ = knowledge_gate
    monkeypatch.setattr('core.turns.get_turn_service', lambda *a: pytest.fail('Fenced work initialized the ledger'))
    setattr(gate, fence, 'Synthetic recovery fault' if fence == 'fault' else True)
    with pytest.raises(KnowledgeError):
        with service.changing(''):
            pytest.fail('Fenced mutation entered')
    assert getattr(gate, fence)


def test_drain_during_accepted_maintenance_remains_latched_after_it_finishes(knowledge_gate):
    gate, service, _ = knowledge_gate
    with service.changing(''):
        worker = threading.Thread(target=gate.begin_shutdown)
        worker.start()
        worker.join(timeout=2)
        assert not worker.is_alive() and gate.draining and gate.maintenance
    assert gate.draining and not gate.maintenance
    with pytest.raises(KnowledgeError):
        with service.changing(''):
            pytest.fail('Draining must not resume new maintenance')


def test_unclosed_thread_refuses_knowledge_before_lazy_initialization(knowledge_gate, monkeypatch):
    gate, service, _ = knowledge_gate
    entered, release, local = threading.Event(), threading.Event(), threading.local()
    class PrivateTail:
        def __del__(self):
            entered.set()
            release.wait(5)
    worker = gate.start_job(lambda: setattr(local, 'value', PrivateTail()))
    try:
        assert entered.wait(3) and not gate.active and gate.owned_jobs == 1
        monkeypatch.setattr('core.turns.get_turn_service', lambda *a: pytest.fail('Unclosed job initialized the ledger'))
        with pytest.raises(KnowledgeError):
            with service.changing(''):
                pytest.fail('Unclosed thread admitted knowledge maintenance')
        assert not gate.maintenance
    finally:
        release.set()
        assert gate.join_jobs(timeout=3) and not worker.is_alive()
    monkeypatch.setattr('core.turns.get_turn_service', get_turn_service)
    with service.changing(''):
        assert gate.maintenance and gate.owned_jobs == 0
