"""HTTP snapshots may borrow a live host lease without releasing its owner."""
from types import SimpleNamespace
import threading

from flask import Blueprint, Flask, Response, g, jsonify
import pytest

from core import state_gate


@pytest.fixture
def page(monkeypatch):
    gate = state_gate.StateGate()
    monkeypatch.setattr(state_gate, 'STATE', gate)
    app = Flask(__name__)
    app.config.update(TESTING=True)
    calls = []

    @app.get('/snapshot')
    def snapshot():
        calls.append('snapshot')
        with gate.exclusive(allow_lease=state_gate.request_lease()):
            return jsonify(active=len(gate.active), key=g.altera_state_lease.key, maintenance=gate.maintenance)

    @app.get('/stream')
    def stream():
        return Response(iter([b'first', b'second']))

    @app.get('/raise')
    def failure():
        raise RuntimeError('Synthetic route error')

    @app.post('/write')
    def write():
        return jsonify(key=g.altera_state_lease.key)

    @app.get('/state')
    def state():
        return jsonify(active=len(gate.active), borrowed=getattr(g, 'altera_state_lease_borrowed', False))

    backups = Blueprint('backups', __name__)

    @backups.get('/backup-snapshot')
    def backup_snapshot():
        with gate.exclusive(allow_lease=state_gate.request_lease(gate)):
            return jsonify(maintenance=True)

    @backups.get('/backup-read')
    def backup_read():
        calls.append('backup-read')
        return jsonify(active=len(gate.active))

    @backups.get('/backup-stream')
    def backup_stream():
        return Response(iter([b'first', b'second']))

    app.register_blueprint(backups)
    state_gate.install_state_gate(app)
    return SimpleNamespace(gate=gate, app=app, client=app.test_client(), calls=calls)












def test_ordinary_request_retains_existing_stream_and_snapshot_rules(page):
    result = page.client.get('/snapshot')
    assert result.status_code == 200 and not page.gate.active
    stream = page.client.get('/stream', buffered=False)
    assert len(page.gate.active) == 1
    stream.close()
    assert not page.gate.active




def test_ordinary_backup_owns_lease_and_maintenance_blocks_before_route(page):
    assert page.client.get('/backup-read').json == {'active': 1}
    assert page.calls == ['backup-read'] and not page.gate.active
    with page.gate.exclusive():
        assert page.client.get('/backup-read').status_code == 503
        assert page.calls == ['backup-read']
    assert page.client.get('/backup-snapshot').status_code == 200
    assert not page.gate.active and not page.gate.maintenance


def test_backup_stream_keeps_source_busy_until_response_closes(page):
    response = page.client.get('/backup-stream', buffered=False)
    assert next(iter(response.response)) == b'first'
    with pytest.raises(state_gate.StateBusy):
        with page.gate.exclusive():
            pytest.fail('An open backup stream is still owned work.')
    response.close()
    assert not page.gate.active
    with page.gate.exclusive():
        pass


def test_stream_exhaustion_retains_lease_through_actual_source_close():
    gate = state_gate.StateGate()
    closing, release = threading.Event(), threading.Event()
    class Source:
        def __iter__(self):
            return iter(())
        def close(self):
            closing.set()
            assert release.wait(5)
    source = Source()
    stream = state_gate.LeasedIterator(source, gate.enter('synthetic-stream'))
    worker = threading.Thread(target=lambda: list(stream))
    worker.start()
    try:
        assert closing.wait(5) and gate.active
        with pytest.raises(state_gate.StateBusy):
            with gate.exclusive():
                pytest.fail('Source cleanup has not finished.')
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and not gate.active
    stream.close()  # Closing an already-finished stream is harmless.


def test_unconfirmed_stream_close_fences_maintenance():
    gate = state_gate.StateGate()
    class Source:
        def __iter__(self):
            return iter(())
        def close(self):
            raise OSError('synthetic close error')
    stream = state_gate.LeasedIterator(Source(), gate.enter('synthetic-stream'))
    with pytest.raises(OSError, match='synthetic close error'):
        list(stream)
    assert gate.draining and not gate.active
    with pytest.raises(state_gate.StateBusy):
        with gate.exclusive():
            pytest.fail('Unconfirmed cleanup must fence maintenance.')
    stream.close()


def test_stream_closes_an_owned_iterator_when_its_container_has_no_close():
    gate = state_gate.StateGate()
    closed = []
    class Source:
        def __iter__(self):
            try:
                yield b'synthetic'
            finally:
                closed.append(True)
    stream = state_gate.LeasedIterator(Source(), gate.enter('synthetic-stream'))
    assert next(stream) == b'synthetic' and gate.active
    stream.close()
    assert closed == [True] and not gate.active and not gate.draining


def test_stream_closes_the_actual_iterator_and_its_distinct_source_before_releasing_the_lease():
    gate = state_gate.StateGate()
    lease = gate.enter('synthetic-stream')
    closed = []

    class Source:
        def __iter__(self):
            try:
                yield b'synthetic'
            finally:
                assert lease.key in gate.active
                closed.append('iterator')

        def close(self):
            assert lease.key in gate.active
            closed.append('source')

    stream = state_gate.LeasedIterator(Source(), lease)
    assert next(stream) == b'synthetic'
    stream.close()
    stream.close()
    assert closed == ['iterator', 'source'] and not gate.active and not gate.draining


def test_failed_iterator_creation_closes_the_source_and_releases_the_lease():
    gate = state_gate.StateGate()
    lease = gate.enter('synthetic-stream')
    closed = []

    class Source:
        def __iter__(self):
            raise OSError('synthetic iteration error')

        def close(self):
            assert lease.key in gate.active
            closed.append('source')

    with pytest.raises(OSError, match='synthetic iteration error'):
        state_gate.LeasedIterator(Source(), lease)
    assert closed == ['source'] and not gate.active and not gate.draining


def test_closed_stream_cannot_resume_an_unclosable_iterator():
    gate = state_gate.StateGate()
    stream = state_gate.LeasedIterator([b'first', b'second'], gate.enter('synthetic-stream'))
    assert next(stream) == b'first'
    stream.close()
    with pytest.raises(StopIteration):
        next(stream)
    assert not gate.active


def test_faulted_recovery_backup_can_enter_but_shutdown_still_fences_it(page):
    page.gate.fail_closed('Synthetic recovery required')
    assert page.client.get('/backup-read').status_code == 200
    assert page.client.get('/backup-snapshot').status_code == 200
    assert page.client.get('/state').status_code == 503
    assert not page.gate.active
    page.gate.begin_shutdown()
    assert page.client.get('/backup-read').status_code == 503
    assert page.client.get('/backup-snapshot').status_code == 503






def test_request_allowance_is_absent_outside_a_request_and_for_another_gate(page):
    assert state_gate.request_lease() is None
    outer = page.gate.enter('other-host-http')
    with page.app.test_request_context('/'):
        g.altera_state_lease = outer
        assert state_gate.request_lease() is outer
        assert state_gate.request_lease(state_gate.StateGate()) is None
        outer.release()
        assert state_gate.request_lease() is None
