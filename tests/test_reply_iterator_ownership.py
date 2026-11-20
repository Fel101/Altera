"""Reply iterators retain their resources through completion and failure."""
import threading
from types import SimpleNamespace

import pytest
from flask import Flask

from core import state_gate
from core.datastore import DataStore
from core.owned_iterator import owned_iterator
from core.turns import TurnService

def test_early_exit_closes_iterator_and_source():
    closed = []
    class Iterator:
        def __iter__(self):
            return self
        def __next__(self):
            return 'value'
        def close(self):
            closed.append('iterator')
    class Source:
        def __iter__(self):
            return Iterator()
        def close(self):
            closed.append('source')
    with owned_iterator(Source()) as items:
        assert next(items) == 'value'
    assert closed == ['iterator', 'source']

@pytest.fixture
def reply_world(tmp_path, monkeypatch):
    import core.episodes
    import core.voice_notes
    store = DataStore(str(tmp_path / 'synthetic.db'))
    sid = store.create_session('Synthetic')
    service = TurnService(store)
    app = Flask(__name__)
    gate = state_gate.StateGate()
    monkeypatch.setattr(state_gate, 'STATE', gate)
    monkeypatch.setattr(core.episodes, 'get_episode_service', lambda store: SimpleNamespace(release_recall=lambda key: None))
    monkeypatch.setattr(core.voice_notes, 'settle_linked_turn', lambda *args: None)
    return store, sid, service, app, gate


def test_reply_job_remains_owned_until_delayed_iterator_close(reply_world):
    store, sid, service, app, gate = reply_world
    entered, release = threading.Event(), threading.Event()
    class Stream:
        def __iter__(self):
            return self
        def __next__(self):
            return {'error': 'synthetic failed reply'}
        def close(self):
            entered.set()
            assert release.wait(10)
    service.submit('owned-reply-close-001', sid, 'Synthetic', False, lambda _: Stream(), app)
    try:
        assert entered.wait(5)
        gate.begin_shutdown()
        assert gate.owned_jobs == 1 and not gate.join_jobs(timeout=0)
    finally:
        release.set()
    assert gate.join_jobs(timeout=10)
    assert service.get('owned-reply-close-001')['status'] == 'failed'
