"""Failure, persistence and reconnect checks; all stores/models are isolated."""
import json
import threading
from types import SimpleNamespace

import pytest
from flask import Flask

from core.request_security import install_request_security
from core.turns import CURRENT_TURN, TurnError, TurnService, get_turn_service
from test_blueprints import create_app
from test_companion_api import engine, tmp_store, world  # noqa: F401


def decoded(response):
    return [json.loads(line[6:]) for line in response.get_data(as_text=True).splitlines() if line.startswith('data: ')]


@pytest.fixture
def reliable(world, engine, monkeypatch, altera_config):
    from core.routes import core_api
    import core.emotion
    monkeypatch.setattr(core_api, '_get_chat_engine', lambda: engine)
    monkeypatch.setattr(core.emotion, 'analyze_and_store_emotion', lambda *a, **kw: None)
    altera_config.write_text('voice:\n  tts_enabled: false\n', encoding='utf-8')
    return create_app('core_api').test_client()


def test_replay_saves_exactly_one_pair_and_rejects_key_reuse(reliable, world, engine):
    payload = {'message': 'Hello there', 'request_id': 'replay-request-123'}
    first = decoded(reliable.post('/api/chat/stream', json=payload))
    second = decoded(reliable.post('/api/chat/stream', json=payload))
    assert first[-1]['status'] == second[-1]['status'] == 'saved'
    sid = first[-1]['session_id']
    assert len(world.store.get_messages(sid)) == 2
    assert len(engine.llm.calls) == 1
    assert first[-1]['message_id'] == second[-1]['message_id']
    assert reliable.post('/api/chat/stream', json={**payload, 'message': 'Different'}).status_code == 409
    status = reliable.get('/api/chat/requests/replay-request-123')
    assert status.headers['Cache-Control'] == 'no-store'
    assert all(name not in status.json for name in ('process_id', 'fingerprint', 'expression_pending', 'memory_pending'))
    metrics = json.loads(get_turn_service(world.store).get(payload['request_id'])['diagnostics'])
    assert metrics['context']['limit_tokens'] > 0
    assert metrics['context']['limit_source'] in {'configuration', 'provider metadata', 'fallback assumption'}


def test_lost_browser_does_not_cancel_or_duplicate_the_reply(reliable, world, engine):
    gate = threading.Event()
    def chunks():
        yield 'First '
        assert gate.wait(timeout=5)
        yield 'second'
    engine.llm.chunks = chunks()
    payload = {'message': 'Hi', 'request_id': 'disconnected-123'}
    first = reliable.post('/api/chat/stream', json=payload, buffered=False)
    first.close()
    gate.set()
    replay = decoded(reliable.post('/api/chat/stream', json=payload))
    assert replay[-1]['status'] == 'saved'
    assert ''.join(e.get('response') or '' for e in replay) == 'First second'
    assert len(engine.llm.calls) == 1


def test_partial_model_failure_is_durable_and_never_reports_saved(reliable, world, engine):
    def broken():
        yield 'Partial'
        raise ConnectionError('private internal address')
    engine.llm.chunks = broken()
    payload = {'message': 'Hi', 'request_id': 'failed-request-123'}
    events = decoded(reliable.post('/api/chat/stream', json=payload))
    assert events[-1]['status'] == 'failed' and events[-1]['response'] == 'Partial'
    assert events[-1]['user_message_id']
    assert not any(e.get('status') in {'final', 'done', 'saved'} for e in events)
    assert 'private internal' not in json.dumps(events)
    assert [m.role for m in world.store.get_messages(events[-1]['session_id'])] == ['user']
    history = reliable.get(f"/api/messages/{events[-1]['session_id']}").json
    assert history[-1]['draft'] is True and history[-1]['reply_state'] == 'failed'
    assert history[-1]['content'] == 'Partial'
    decoded(reliable.post('/api/chat/stream', json=payload))
    assert len(engine.llm.calls) == 1


def test_reply_event_failure_closes_the_nested_model_iterator(reliable, world, engine, monkeypatch):
    service = get_turn_service(world.store)
    original_event = service._event
    class Stream:
        def __init__(self):
            self.closed, self.sent = False, False
        def __iter__(self):
            return self
        def __next__(self):
            if self.sent:
                raise StopIteration
            self.sent = True
            return 'Synthetic partial reply'
        def close(self):
            self.closed = True
    stream = Stream()
    engine.llm.chunks = stream
    def fail_response_event(request_id, event):
        if event.get('response'):
            raise OSError('synthetic event-store failure')
        return original_event(request_id, event)
    monkeypatch.setattr(service, '_event', fail_response_event)
    result = decoded(reliable.post('/api/chat/stream', json={'message': 'Hello', 'request_id': 'owned-chain-failure-123'}))
    assert result[-1]['status'] == 'failed' and stream.closed




def test_persistence_failure_never_confirms_saved(reliable, world, monkeypatch):
    original = world.store.save_turn_message
    def fail_assistant(request_id, sid, role, content, **kwargs):
        if role == 'assistant':
            raise OSError('disk full')
        return original(request_id, sid, role, content, **kwargs)
    monkeypatch.setattr(world.store, 'save_turn_message', fail_assistant)
    events = decoded(reliable.post('/api/chat/stream', json={'message': 'Hi', 'request_id': 'disk-failure-123'}))
    assert events[-1]['status'] == 'failed'
    assert not events[-1]['message_id']


@pytest.mark.parametrize('column', ['user_message_id', 'assistant_message_id'])
def test_failed_turn_link_rolls_back_its_message(reliable, world, column):
    with world.store._connect() as conn:
        conn.execute(f'''CREATE TRIGGER fail_turn_link BEFORE UPDATE OF {column} ON chat_turns
                         WHEN OLD.{column} IS NULL AND NEW.{column} IS NOT NULL
                         BEGIN SELECT RAISE(ABORT, 'synthetic ledger failure'); END''')
    events = decoded(reliable.post('/api/chat/stream', json={'message': 'Hi', 'request_id': 'atomic-link-123'}))
    last = events[-1]
    assert last['status'] == 'failed'
    messages = world.store.get_messages(last['session_id'])
    assert [message.role for message in messages] == ([] if column == 'user_message_id' else ['user'])
    assert last['message_id'] is None
    assert (last['user_message_id'] is None) == (column == 'user_message_id')


def test_restarted_turn_with_committed_answer_is_saved(tmp_store):
    service = get_turn_service()
    sid = tmp_store.create_session('old')
    saved = tmp_store.save_message(sid, 'assistant', 'Already saved')
    with service.connection() as conn:
        conn.execute("""INSERT INTO chat_turns(request_id,session_id,fingerprint,process_id,status,response,assistant_message_id)
                        VALUES(?,?,?,'old-process','streaming','Already saved',?)""",
                     ('old-saved-request-123', sid, 'hash', saved))
    restarted = TurnService(tmp_store)
    assert restarted.get('old-saved-request-123')['status'] == 'saved'
    assert list(restarted.events('old-saved-request-123'))[-1]['message_id'] == saved


def test_restart_does_not_accept_an_answer_from_another_session(tmp_store):
    service = get_turn_service()
    sid = tmp_store.create_session('old')
    other = tmp_store.create_session('other')
    foreign = tmp_store.save_message(other, 'assistant', 'Other conversation')
    with service.connection() as conn:
        conn.execute("""INSERT INTO chat_turns(request_id,session_id,fingerprint,process_id,status,assistant_message_id)
                        VALUES(?,?,?,'old-process','streaming',?)""", ('old-foreign-request-123', sid, 'hash', foreign))
    assert TurnService(tmp_store).get('old-foreign-request-123')['status'] == 'interrupted'


@pytest.mark.parametrize('exception', [InterruptedError('stopped'), OSError('synthetic follow-up failure')])
def test_failure_after_saved_answer_does_not_report_a_failed_turn(tmp_store, exception):
    service = get_turn_service()
    sid = tmp_store.create_session('chat')
    def produce(session_id):
        turn = CURRENT_TURN.get()
        mid = tmp_store.save_turn_message(turn.request_id, session_id, 'assistant', 'Already saved')
        assert mid
        yield {'response': 'Already saved'}
        raise exception
    service.submit('follow-up-failure-123', sid, 'hi', False, produce, Flask(__name__))
    result = list(service.events('follow-up-failure-123'))[-1]
    assert result['status'] == 'saved' and result['message_id']
    assert tmp_store.get_message(result['message_id']).content == 'Already saved'


@pytest.mark.parametrize('exception', [None, InterruptedError('stopped'), OSError('synthetic late failure')])
def test_saved_answer_records_exchange_even_after_late_failure(tmp_store, monkeypatch, exception):
    from core import episodes, expression, memory, memory_recovery
    service = get_turn_service()
    sid = tmp_store.create_session('chat')
    calls = []
    episode_service = SimpleNamespace(
        record_recall=lambda *args, **kwargs: calls.append(('episodes', args, kwargs)),
        release_recall=lambda request_id: None,
    )
    monkeypatch.setattr(episodes, 'get_episode_service', lambda store: episode_service)
    monkeypatch.setattr(memory, 'get_memory_manager', lambda: SimpleNamespace(
        record_recalled=lambda *args: calls.append(('memory', args, {}))))
    monkeypatch.setattr(memory_recovery, 'consume_pending', lambda service, request_id, manager: calls.append(('memory', (), {})))
    monkeypatch.setattr(expression, 'consume_pending', lambda store, request_id: calls.append(('expression', (store, request_id), {})))
    def produce(session_id):
        turn = CURRENT_TURN.get()
        tmp_store.save_turn_message(turn.request_id, session_id, 'user', 'Question')
        tmp_store.save_turn_message(turn.request_id, session_id, 'assistant', 'Answer')
        yield {'response': 'Answer'}
        if exception is not None:
            raise exception
    service.submit('saved-exchange-123', sid, 'Question', False, produce, Flask(__name__))
    result = list(service.events('saved-exchange-123'))[-1]
    assert result['status'] == 'saved'
    assert [name for name, _, _ in calls] == ['episodes', 'memory', 'expression']
    assert calls[0][2]['request_id'] == 'saved-exchange-123'
    assert calls[2][1][1] == 'saved-exchange-123'


def test_saved_exchange_callback_interrupt_does_not_strand_reply_job(tmp_store, monkeypatch):
    from core import episodes
    service = get_turn_service()
    sid = tmp_store.create_session('chat')
    released = []
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt()
    episode_service = SimpleNamespace(record_recall=interrupt, release_recall=lambda request_id: released.append(request_id))
    monkeypatch.setattr(episodes, 'get_episode_service', lambda store: episode_service)
    def produce(session_id):
        turn = CURRENT_TURN.get()
        tmp_store.save_turn_message(turn.request_id, session_id, 'user', 'Question')
        tmp_store.save_turn_message(turn.request_id, session_id, 'assistant', 'Answer')
        yield {'response': 'Answer'}
    service.submit('saved-callback-interrupted-123', sid, 'Question', False, produce, Flask(__name__))
    assert list(service.events('saved-callback-interrupted-123'))[-1]['status'] == 'saved'
    assert released == ['saved-callback-interrupted-123']
    assert 'saved-callback-interrupted-123' not in service.jobs


def test_previous_process_turns_become_interrupted(tmp_store):
    service = get_turn_service()
    sid = tmp_store.create_session('old')
    with service.connection() as conn:
        conn.execute("INSERT INTO chat_turns(request_id,session_id,fingerprint,process_id,status,response) VALUES(?,?,?,'old-process','streaming','partial')",
                     ('old-request-123', sid, 'hash'))
    restarted = TurnService(tmp_store)
    assert restarted.get('old-request-123')['status'] == 'interrupted'
    assert list(restarted.events('old-request-123'))[-1]['response'] == 'partial'


def test_one_active_turn_per_session_and_repeated_id_joins(tmp_store):
    service = get_turn_service()
    sid = tmp_store.create_session('chat')
    gate = threading.Event()
    def produce(session_id):
        assert gate.wait(5)
        mid = tmp_store.save_message(session_id, 'assistant', 'Done')
        CURRENT_TURN.get().update(assistant_message_id=mid)
        yield {'response': 'Done'}
    app = Flask(__name__)
    service.submit('busy-request-123', sid, 'hi', False, produce, app)
    try:
        assert service.submit('busy-request-123', sid, 'hi', False, produce, app)['session_id'] == sid
        with pytest.raises(TurnError):
            service.submit('other-request-123', sid, 'other', False, produce, app)
    finally:
        gate.set()
        assert list(service.events('busy-request-123'))[-1]['status'] == 'saved'


@pytest.mark.parametrize('payload', [[], {'message': 12}, {'message': '\u0000'}, {'message': 'Hi', 'request_id': None}])
def test_chat_input_errors_are_400(reliable, payload):
    assert reliable.post('/api/chat/stream', json=payload).status_code == 400


@pytest.mark.parametrize('headers', [{'Origin': 'http://other.example'}, {'Origin': 'http://localhost:99'},
                                    {'Origin': 'http://['}, {'Origin': 'null'}, {'Sec-Fetch-Site': 'cross-site'}])
def test_local_browser_writes_share_the_origin_guard(headers):
    app = Flask(__name__)
    install_request_security(app)
    app.add_url_rule('/write', view_func=lambda: 'ok', methods=['POST'])
    assert app.test_client().post('/write', headers=headers).status_code == 403
    assert app.test_client().post('/write', headers={'Origin': 'http://localhost'}).status_code == 200


@pytest.mark.parametrize('payload', [{'content': ''}, {'content': 42}, {'content': 'A', 'confidence': 'high'},
                                    {'content': 'A', 'confidence': 'NaN'}, {'content': 'A', 'confidence': 1.1},
                                    {'content': 'A', 'confidence': True}])
def test_invalid_memory_is_rejected_without_loading_chroma(reliable, payload):
    assert reliable.post('/api/memory', json=payload).status_code == 400


def test_memory_edit_delegates_owner_and_validates_confidence(reliable, monkeypatch):
    from core.routes import core_api
    updates = []
    collection = SimpleNamespace(get=lambda **kw: {'metadatas': [{'avatar_id': 'mara', 'type': 'fact'}]})
    manager = SimpleNamespace(store=SimpleNamespace(collection=collection))
    service = SimpleNamespace(change=lambda *args, **kw: updates.append((args, kw)) or {'status': 'ok'})
    monkeypatch.setattr(core_api, 'get_memory_manager', lambda: manager)
    monkeypatch.setattr(core_api, '_knowledge_manager', lambda actual: service if actual is manager else None)
    assert reliable.put('/api/memory/m1', json={'confidence': 'high'}).status_code == 400
    assert updates == []
    assert reliable.put('/api/memory/m1', json={'content': 'Corrected', 'confidence': 0.9}).status_code == 200
    assert updates == [(('mara', 'm1'), {'content': 'Corrected', 'confidence': 0.9, 'new_owner': None})]


def test_clear_and_session_delete_remove_durable_reply_text(reliable, world):
    events = decoded(reliable.post('/api/chat/stream', json={'message': 'Hi', 'request_id': 'delete-request-123'}))
    sid = events[-1]['session_id']
    world.store.delete_session(sid)
    assert get_turn_service().get('delete-request-123') is None
    with get_turn_service().connection() as conn:
        assert conn.execute('SELECT count(*) FROM chat_turn_events').fetchone()[0] == 0
