"""Retrieval priority never substitutes for confidence or source eligibility."""
from contextlib import contextmanager
from datetime import datetime, timedelta, UTC
import hashlib

import pytest

from core import memory
from core.knowledge import KnowledgeError, floor
from core.salience import describe, lexical_relevance
from test_knowledge_controls import knowledge_world  # noqa: F401
from test_companion_api import engine, tmp_store, world  # noqa: F401

NOW = datetime(2026, 10, 28, 12, tzinfo=UTC)


def metadata(w, identifier):
    return w.manager.store.collection.get(ids=[identifier])['metadatas'][0]


def test_decay_changes_priority_without_deleting_or_changing_confidence():
    values = {'salience': 4, 'confidence': .12, 'created_at': (NOW - timedelta(days=30)).isoformat()}
    result = describe(values, NOW, .8)
    assert result['decay_factor'] == .5 and not result['confidence_used'] and not result['deletes_memory']
    assert describe({**values, 'confidence': .99}, NOW, .8) == result
    assert describe(values, NOW + timedelta(days=3650))['decay_factor'] == .2
    assert values['confidence'] == .12


def test_pin_stops_age_decay_and_legacy_naive_time_stays_explicitly_unknown():
    assert describe({'pinned': True, 'created_at': '2001-01-01T00:00:00+00:00'}, NOW)['decay_factor'] == 1
    legacy = describe({'created_at': '2001-01-01T00:00:00'}, NOW)
    assert legacy['age_days'] is None and legacy['age_basis'] == 'unknown_legacy_time' and legacy['decay_factor'] == 1
    recent = describe({'created_at': '2001-01-01T00:00:00+00:00', 'last_recalled_at': NOW.isoformat()}, NOW)
    assert recent['age_days'] == 0 and recent['age_basis'] == 'last_recalled_at'


@pytest.mark.parametrize('bad', [None, {}, {'salience': True}, {'salience': 99, 'pinned': 'true'},
                                 {'created_at': 'broken'}, {'last_recalled_at': '999999999999999999'}])
def test_malformed_priority_metadata_is_neutral_and_finite(bad):
    value = describe(bad, NOW)
    assert value['salience'] == 3 and value['ranking_score'] is None and .2 <= value['decay_factor'] <= 1
    assert not value['pinned']


def test_naive_clock_is_rejected_and_lexical_pins_require_non_generic_words():
    with pytest.raises(ValueError): describe({}, datetime(2026, 10, 28))
    assert lexical_relevance('what do you remember about me', 'I have a dog') == 0
    assert lexical_relevance('jasmine tea', 'I prefer jasmine tea.') == 1


def test_priority_write_is_separate_from_fact_confidence_and_recall_boundary(knowledge_world):
    w = knowledge_world
    identifier = w.service.remember(w.a, 'A saved preference', confidence=.73)
    epoch = w.ds.dataset_epoch()['value']; before = dict(metadata(w, identifier))
    sha = hashlib.sha256(b'A saved preference').hexdigest()
    result = w.service.prioritize(w.a, identifier, epoch=epoch, revision=0, content_sha256=sha, changes={'pinned': True, 'salience': 5})
    after = metadata(w, identifier)
    assert result['priority']['pinned'] and result['priority']['salience'] == 5
    assert after['confidence'] == .73 and after['updated_at'] == before['updated_at'] and floor(w.ds, w.a) == 0
    assert w.manager.store.collection.get(ids=[identifier])['documents'] == ['A saved preference']
    with pytest.raises(KnowledgeError): w.service.prioritize(w.a, identifier, epoch=epoch, revision=0, content_sha256=sha, changes={'pinned': False})
    with pytest.raises(KnowledgeError): w.service.prioritize(w.b, identifier, epoch=epoch, revision=1, content_sha256=sha, changes={'pinned': False})
    with pytest.raises(KnowledgeError): w.service.prioritize(w.a, identifier, epoch='old', revision=1, content_sha256=sha, changes={'pinned': False})
    with pytest.raises(KnowledgeError): w.service.prioritize(w.a, identifier, epoch=epoch, revision=1, content_sha256='changed', changes={'pinned': False})


def test_epoch_is_rechecked_inside_the_priority_mutation_lease(knowledge_world, monkeypatch):
    w = knowledge_world; identifier = w.service.remember(w.a, 'Tea')
    epoch = w.ds.dataset_epoch()['value']; original = w.service.changing
    @contextmanager
    def rotated(*args):
        with original(*args):
            w.ds.set_key('dataset_epoch', {'value': 'new'})
            yield
    monkeypatch.setattr(w.service, 'changing', rotated)
    with pytest.raises(KnowledgeError, match='data changed'):
        w.service.prioritize(w.a, identifier, epoch=epoch, revision=0, content_sha256=hashlib.sha256(b'Tea').hexdigest(), changes={'pinned': True})
    assert metadata(w, identifier).get('pinned') is False


def test_successful_context_inclusion_is_idempotent_and_does_not_touch_fact_timestamp(knowledge_world):
    w = knowledge_world; identifier = w.service.remember(w.a, 'Tea')
    w.ds.dataset_epoch()
    item = w.service.get(w.a, identifier); before = dict(metadata(w, identifier))
    captured = w.manager.capture_recall(w.ds, w.a, [item])
    user = w.ds.save_message(w.a_sid, 'user', 'Tell me about tea')
    assistant = w.ds.save_message(w.a_sid, 'assistant', 'A saved answer')
    assert w.manager.record_recalled(w.ds, captured, user, assistant, NOW) == 1
    assert w.manager.record_recalled(w.ds, captured, user, assistant, NOW + timedelta(hours=1)) == 0
    after = metadata(w, identifier)
    assert after['recall_count'] == 1 and after['last_recalled_at'] == NOW.isoformat()
    assert after['confidence'] == before['confidence'] and after['updated_at'] == before['updated_at']
    other_user = w.ds.save_message(w.b_sid, 'user', 'Other companion')
    other_reply = w.ds.save_message(w.b_sid, 'assistant', 'Other reply')
    assert w.manager.record_recalled(w.ds, captured, other_user, other_reply, NOW) == 0
    w.ds.set_key('dataset_epoch', {'value': 'new'})
    assert w.manager.record_recalled(w.ds, captured, user, assistant, NOW) == 0


def test_changed_content_or_source_is_never_strengthened_by_old_prompt_capture(knowledge_world):
    w = knowledge_world; identifier = w.service.remember(w.a, 'Tea'); w.ds.dataset_epoch()
    captured = w.manager.capture_recall(w.ds, w.a, [w.service.get(w.a, identifier)])
    user = w.ds.save_message(w.a_sid, 'user', 'Tea'); assistant = w.ds.save_message(w.a_sid, 'assistant', 'Saved')
    w.manager.store.collection.update(ids=[identifier], documents=['Coffee'])
    assert w.manager.record_recalled(w.ds, captured, user, assistant, NOW) == 0
    assert metadata(w, identifier).get('recall_count', 0) == 0


def pending_recall(w):
    import json
    from core.turns import PROCESS_ID
    identifier = w.service.remember(w.a, 'Synthetic tea preference')
    captured = w.manager.capture_recall(w.ds, w.a, [w.service.get(w.a, identifier)])
    user = w.ds.save_message(w.a_sid, 'user', 'Synthetic user question')
    with w.ds._connect() as conn:
        conn.execute('INSERT INTO chat_turns(request_id,session_id,fingerprint,process_id,status,user_message_id) VALUES(?,?,?,?,?,?)',
                     ('pending-memory-123', w.a_sid, 'synthetic', PROCESS_ID, 'streaming', user))
    assistant = w.ds.save_turn_message('pending-memory-123', w.a_sid, 'assistant', 'Synthetic saved answer',
                                       captured_memories=captured)
    with w.ds._connect() as conn:
        raw = conn.execute("SELECT memory_pending FROM chat_turns WHERE request_id='pending-memory-123'").fetchone()[0]
        assert raw and 'Synthetic tea preference' not in raw and 'Synthetic user question' not in raw
        intent = json.loads(raw)
        conn.execute("UPDATE chat_turns SET process_id='retired-process' WHERE request_id='pending-memory-123'")
    return identifier, user, assistant, intent


def test_memory_intent_rejects_malformed_or_unbound_captures(knowledge_world):
    import copy
    import json
    from core.memory_recovery import encode_intent
    _, _, _, intent = pending_recall(knowledge_world)
    context = json.dumps(intent['context'])
    captured = intent['capture']
    assert encode_intent(captured, context)
    for change in ('epoch', 'owner', 'floor', 'hash', 'duplicate', 'extra'):
        value = copy.deepcopy(captured)
        if change == 'epoch':
            value['dataset_epoch'] = 'different'
        elif change == 'owner':
            value['avatar_id'] = 'different'
        elif change == 'floor':
            value['recall_after'] += 1
        elif change == 'hash':
            value['items'][0]['sha256'] = 'not-a-hash'
        elif change == 'duplicate':
            value['items'].append(value['items'][0])
        else:
            value['unexpected'] = 'sensitive text'
        assert encode_intent(value, context) is None


@pytest.mark.parametrize('change', ['none', 'user', 'assistant', 'memory', 'owner', 'epoch', 'floor', 'forgotten'])
def test_restart_recovers_only_unchanged_source_backed_recency(knowledge_world, change):
    from core.memory_recovery import recover_pending
    from core.turns import TurnService, get_turn_service
    w = knowledge_world
    identifier, user, assistant, intent = pending_recall(w)
    if change == 'memory':
        w.manager.store.collection.update(ids=[identifier], documents=['Synthetic changed preference'])
    elif change == 'forgotten':
        w.manager.store.collection.delete(ids=[identifier])
    elif change == 'epoch':
        w.ds.set_key('dataset_epoch', {'value': 'restored'})
    elif change == 'floor':
        w.ds.set_key('knowledge_recall:' + w.a, {'through': assistant})
    elif change in {'user', 'assistant', 'owner'}:
        with w.ds._connect() as conn:
            if change == 'owner':
                conn.execute('UPDATE sessions SET avatar_id=? WHERE id=?', (w.b, w.a_sid))
            else:
                conn.execute('UPDATE messages SET content=? WHERE id=?', ('Changed source', user if change == 'user' else assistant))
    TurnService(w.ds)  # Reconcile the previously saved answer as normal startup does.
    recover_pending(get_turn_service(w.ds))
    recover_pending(get_turn_service(w.ds))
    with w.ds._connect() as conn:
        assert conn.execute("SELECT memory_pending FROM chat_turns WHERE request_id='pending-memory-123'").fetchone()[0] is None
    if change != 'forgotten':
        after = metadata(w, identifier)
        assert after.get('recall_count', 0) == (1 if change == 'none' else 0)
        if change == 'none':
            assert after['last_recalled_at'] == intent['context']['saved_at']


def test_partial_vector_update_retries_without_double_counting(knowledge_world, monkeypatch):
    from core.memory_recovery import recover_pending
    from core.turns import TurnService, get_turn_service
    w = knowledge_world
    identifier, _, _, _ = pending_recall(w)
    TurnService(w.ds)
    collection = w.manager.store.collection
    original = collection.update
    calls = []
    def update_then_fail(*args, **kwargs):
        original(*args, **kwargs)
        calls.append(True)
        raise OSError('synthetic lost vector acknowledgment')
    monkeypatch.setattr(collection, 'update', update_then_fail)
    recover_pending(get_turn_service(w.ds))
    assert calls and metadata(w, identifier)['recall_count'] == 1
    with w.ds._connect() as conn:
        assert conn.execute("SELECT memory_pending FROM chat_turns WHERE request_id='pending-memory-123'").fetchone()[0]
    monkeypatch.setattr(collection, 'update', original)
    recover_pending(get_turn_service(w.ds))
    assert metadata(w, identifier)['recall_count'] == 1
    with w.ds._connect() as conn:
        assert conn.execute("SELECT memory_pending FROM chat_turns WHERE request_id='pending-memory-123'").fetchone()[0] is None


def test_failed_sqlite_ack_after_vector_write_retries_without_double_counting(knowledge_world):
    from core.memory_recovery import recover_pending
    from core.turns import TurnService, get_turn_service
    w = knowledge_world
    identifier, _, _, _ = pending_recall(w)
    TurnService(w.ds)
    with w.ds._connect() as conn:
        conn.execute("""CREATE TRIGGER fail_memory_ack BEFORE UPDATE OF memory_pending ON chat_turns
                        WHEN OLD.memory_pending IS NOT NULL AND NEW.memory_pending IS NULL
                        BEGIN SELECT RAISE(ABORT, 'synthetic acknowledgment failure'); END""")
    recover_pending(get_turn_service(w.ds))
    assert metadata(w, identifier)['recall_count'] == 1
    with w.ds._connect() as conn:
        assert conn.execute("SELECT memory_pending FROM chat_turns WHERE request_id='pending-memory-123'").fetchone()[0]
        conn.execute('DROP TRIGGER fail_memory_ack')
    recover_pending(get_turn_service(w.ds))
    assert metadata(w, identifier)['recall_count'] == 1
    with w.ds._connect() as conn:
        assert conn.execute("SELECT memory_pending FROM chat_turns WHERE request_id='pending-memory-123'").fetchone()[0] is None


def test_failed_earlier_recovery_does_not_let_later_reply_overtake(knowledge_world, monkeypatch):
    from core.memory_recovery import recover_pending
    from core.turns import PROCESS_ID, TurnService, get_turn_service
    w = knowledge_world
    identifier, _, _, _ = pending_recall(w)
    with w.ds._connect() as conn:
        conn.execute("UPDATE chat_turns SET status='saved' WHERE request_id='pending-memory-123'")
    captured = w.manager.capture_recall(w.ds, w.a, [w.service.get(w.a, identifier)])
    user = w.ds.save_message(w.a_sid, 'user', 'A later synthetic question')
    with w.ds._connect() as conn:
        conn.execute('INSERT INTO chat_turns(request_id,session_id,fingerprint,process_id,status,user_message_id) VALUES(?,?,?,?,?,?)',
                     ('later-memory-123', w.a_sid, 'synthetic', PROCESS_ID, 'streaming', user))
    w.ds.save_turn_message('later-memory-123', w.a_sid, 'assistant', 'A later synthetic answer', captured_memories=captured)
    with w.ds._connect() as conn:
        conn.execute("UPDATE chat_turns SET process_id='retired-process' WHERE request_id='later-memory-123'")
    TurnService(w.ds)
    collection = w.manager.store.collection
    original, calls = collection.update, []
    def fail_once(*args, **kwargs):
        calls.append(True)
        raise OSError('synthetic unavailable vector store')
    monkeypatch.setattr(collection, 'update', fail_once)
    recover_pending(get_turn_service(w.ds))
    assert len(calls) == 1 and metadata(w, identifier).get('recall_count', 0) == 0
    with w.ds._connect() as conn:
        assert conn.execute("SELECT count(*) FROM chat_turns WHERE memory_pending IS NOT NULL").fetchone()[0] == 2
    monkeypatch.setattr(collection, 'update', original)
    recover_pending(get_turn_service(w.ds))
    assert metadata(w, identifier)['recall_count'] == 2


def test_native_vector_recovery_retries_without_embeddings(knowledge_world, tmp_path):
    import chromadb
    from chromadb.api.types import EmbeddingFunction
    from chromadb.config import Settings
    from core.memory_recovery import recover_pending
    from core.turns import PROCESS_ID, TurnService, get_turn_service

    class NoModel(EmbeddingFunction):
        def __init__(self):
            pass
        @staticmethod
        def name():
            return 'altera-recovery-test-no-model'
        def get_config(self):
            return {}
        @staticmethod
        def build_from_config(config):
            return NoModel()
        def __call__(self, input):
            raise AssertionError('Recency recovery must not run embeddings.')

    class LostAcknowledgment:
        def __init__(self, collection):
            self.collection, self.fail = collection, True
        def __getattr__(self, name):
            return getattr(self.collection, name)
        def update(self, *args, **kwargs):
            self.collection.update(*args, **kwargs)
            if self.fail:
                self.fail = False
                raise OSError('synthetic lost vector acknowledgment')

    w = knowledge_world
    client = chromadb.PersistentClient(path=str(tmp_path / 'synthetic-vectors'), settings=Settings(anonymized_telemetry=False))
    collection = client.create_collection('recovery_test', embedding_function=NoModel())
    collection.add(ids=['memory-test'], documents=['Synthetic tea preference'], embeddings=[[1., 0., 0.]],
                   metadatas=[{'avatar_id': w.a, 'user_context': 'manual', 'source_refs': '[]'}])
    wrapped = LostAcknowledgment(collection)
    w.manager.store._collection = wrapped
    captured = w.manager.capture_recall(w.ds, w.a, [{'id': 'memory-test', 'content': 'Synthetic tea preference'}])
    user = w.ds.save_message(w.a_sid, 'user', 'Synthetic question')
    with w.ds._connect() as conn:
        conn.execute('INSERT INTO chat_turns(request_id,session_id,fingerprint,process_id,status,user_message_id) VALUES(?,?,?,?,?,?)',
                     ('native-memory-recovery-123', w.a_sid, 'synthetic', PROCESS_ID, 'streaming', user))
    w.ds.save_turn_message('native-memory-recovery-123', w.a_sid, 'assistant', 'Synthetic answer', captured_memories=captured)
    with w.ds._connect() as conn:
        conn.execute("UPDATE chat_turns SET process_id='retired-process' WHERE request_id='native-memory-recovery-123'")
    TurnService(w.ds)
    recover_pending(get_turn_service(w.ds))
    assert collection.get(ids=['memory-test'])['metadatas'][0]['recall_count'] == 1
    with w.ds._connect() as conn:
        assert conn.execute("SELECT memory_pending FROM chat_turns WHERE request_id='native-memory-recovery-123'").fetchone()[0]
    recover_pending(get_turn_service(w.ds))
    assert collection.get(ids=['memory-test'])['metadatas'][0]['recall_count'] == 1
    with w.ds._connect() as conn:
        assert conn.execute("SELECT memory_pending FROM chat_turns WHERE request_id='native-memory-recovery-123'").fetchone()[0] is None




def test_memory_recovery_holds_gate_until_vector_update_finishes(knowledge_world, monkeypatch):
    import threading
    from core import state_gate
    from core.memory_recovery import recover_pending
    from core.turns import TurnService, get_turn_service
    w = knowledge_world
    pending_recall(w)
    TurnService(w.ds)
    service = get_turn_service(w.ds)
    gate = state_gate.StateGate()
    monkeypatch.setattr(state_gate, 'STATE', gate)
    entered, release = threading.Event(), threading.Event()
    collection = w.manager.store.collection
    original = collection.update
    def slow_update(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        original(*args, **kwargs)
    monkeypatch.setattr(collection, 'update', slow_update)
    worker = threading.Thread(target=lambda: recover_pending(service))
    worker.start()
    try:
        assert entered.wait(5)
        with pytest.raises(state_gate.StateBusy):
            with gate.exclusive():
                pytest.fail('Recovery must own the state gate.')
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and not gate.active


def test_normal_chat_commits_and_consumes_selected_memory_intent(knowledge_world, monkeypatch, altera_config):
    import json
    from core import config
    from core.routes import core_api
    w = knowledge_world
    identifier = w.service.remember(w.a, 'Tea preference')
    monkeypatch.setattr(core_api, '_get_chat_engine', lambda: w.engine)
    altera_config.write_text('voice:\n  tts_enabled: false\n', encoding='utf-8')
    config.reset_cache()
    response = w.client.post('/api/chat/stream', json={'session_id': w.a_sid, 'message': 'Tea preference',
                                                       'request_id': 'normal-memory-recall-123'})
    events = [json.loads(line[6:]) for line in response.get_data(as_text=True).splitlines() if line.startswith('data: ')]
    assert events[-1]['status'] == 'saved'
    assert metadata(w, identifier)['recall_count'] == 1
    with w.ds._connect() as conn:
        assert conn.execute("SELECT memory_pending FROM chat_turns WHERE request_id='normal-memory-recall-123'").fetchone()[0] is None


def test_normal_app_prepare_recovers_prior_memory_intent(knowledge_world, monkeypatch, altera_config):
    from core import runtime
    w = knowledge_world
    identifier, _, _, _ = pending_recall(w)
    if hasattr(w.ds, '_turn_service'):
        del w.ds._turn_service  # Simulate a new process after the old test worker has stopped.
    monkeypatch.setattr(runtime, 'root', lambda: altera_config.parent)
    monkeypatch.setattr(runtime, 'configure_logging', lambda: None)
    runtime.prepare()
    assert metadata(w, identifier)['recall_count'] == 1
    with w.ds._connect() as conn:
        row = conn.execute("SELECT status,memory_pending FROM chat_turns WHERE request_id='pending-memory-123'").fetchone()
        assert row['status'] == 'saved' and row['memory_pending'] is None


def test_ranked_candidates_respect_source_ownership_and_legacy_eligibility(knowledge_world, monkeypatch):
    w = knowledge_world
    first, pinned = (w.service.remember(w.a, text) for text in ('Tea preference', 'Tea preference pinned'))
    legacy = w.manager.store.add_memory('Tea with unknown source', avatar_id=w.a)
    foreign = w.service.remember(w.b, 'Tea foreign secret')
    for identifier in (pinned, legacy, foreign):
        w.manager.store.collection.update(ids=[identifier], metadatas=[{**metadata(w, identifier), 'pinned': True}])
    candidates = [{**w.service.get(w.a, identifier), 'similarity': .8} for identifier in (first, pinned, legacy)]
    candidates.append({**w.service.get(w.b, foreign), 'similarity': 1.})
    requests = []
    monkeypatch.setattr(w.manager.store, 'search', lambda query, limit, avatar_id: requests.append((limit, avatar_id)) or candidates)
    monkeypatch.setattr(w.manager.store, 'pinned', lambda owner, limit: [])
    selected = w.manager.get_relevant_memories('tea', limit=2, avatar_id=w.a)
    assert [m['id'] for m in selected] == [pinned, first]
    assert requests[0][0] <= 40 and requests[0][1] == w.a
    assert all(m['ranking']['confidence_used'] is False for m in selected)


def test_pinned_lookup_is_bounded_metadata_only_and_filters_foreign_rows():
    calls = []
    class Collection:
        def get(self, **kwargs):
            calls.append(kwargs)
            return {'ids': ['a', 'b'], 'documents': ['Tea', 'Other secret'], 'metadatas': [
                {'avatar_id': 'owner', 'pinned': True}, {'avatar_id': 'other', 'pinned': True}]}
        def query(self, **kwargs): raise AssertionError('Listing pins must not embed anything')
    store = memory.MemoryStore.__new__(memory.MemoryStore); store._collection = Collection()
    assert [m['id'] for m in store.pinned('owner', limit=2)] == ['a']
    assert calls[0]['limit'] == 2


def test_user_approved_duplicate_merge_preserves_a_pin_from_the_deleted_duplicate(knowledge_world):
    w = knowledge_world
    best = w.service.remember(w.a, 'I prefer jasmine tea', confidence=.95)
    lower = w.service.remember(w.a, 'I prefer jasmine tea', confidence=.8)
    w.manager.store.collection.update(ids=[lower], metadatas=[{**metadata(w, lower), 'pinned': True, 'salience': 5}])
    items = [w.service.get(w.a, identifier) for identifier in (best, lower)]
    merged = w.manager.deduplicator.consolidate_duplicates([{'type': 'fact', 'memories': items}])
    assert merged[0]['kept'] == best and lower in merged[0]['deleted']
    assert metadata(w, best)['pinned'] is True and metadata(w, best)['salience'] == 5
