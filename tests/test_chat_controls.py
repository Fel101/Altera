"""Chat controls preserve stored conversations under cancellation and failed replacement."""
import threading

from core.turns import get_turn_service
from test_companion_api import engine, tmp_store, world  # noqa: F401
from test_polish_reliability import decoded, reliable  # noqa: F401


def first_reply(client, message='Hello'):
    return decoded(client.post('/api/chat/stream', json={'message': message, 'request_id': 'original-request-123'}))[-1]


def test_regenerate_replaces_in_place_without_asking_for_memories_a_second_time(reliable, engine, world, monkeypatch):
    import core.memory
    scheduled = []

    class Writer:
        sync = True

        def schedule(self, owner):
            scheduled.append(owner)

    monkeypatch.setattr(core.memory, "get_memory_writer", lambda: Writer())
    first = first_reply(reliable)
    engine.llm.chunks = ['A different answer']
    payload = {'session_id': first['session_id'], 'message_id': first['message_id'], 'request_id': 'replacement-request-123'}
    result = decoded(reliable.post('/api/chat/regenerate', json=payload))
    assert result[-1]['status'] == 'saved' and result[-1]['message_id'] == first['message_id']
    messages = world.store.get_messages(first['session_id'])
    assert [m.content for m in messages] == ['Hello', 'A different answer']
    assert len(scheduled) == 1
    assert all(m['content'] != 'Hello!' for m in engine.llm.calls[-1])
    decoded(reliable.post('/api/chat/regenerate', json=payload))
    assert len(engine.llm.calls) == 2
    assert len(scheduled) == 1


def test_failed_replacement_preserves_the_old_answer(reliable, engine, world):
    first = first_reply(reliable)
    def broken():
        yield 'Unfinished replacement'
        raise ConnectionError('offline')
    engine.llm.chunks = broken()
    result = decoded(reliable.post('/api/chat/regenerate', json={
        'session_id': first['session_id'], 'message_id': first['message_id'], 'request_id': 'failed-replacement-123'}))
    assert result[-1]['status'] == 'failed'
    assert world.store.get_message(first['message_id']).content == 'Hello!'
    assert len(world.store.get_messages(first['session_id'])) == 2


def test_failed_replacement_ledger_update_rolls_back_answer_and_invalidation(reliable, engine, world):
    first = first_reply(reliable)
    sid, mid = first['session_id'], first['message_id']
    before_dirty = world.store.get_key(f'knowledge_dirty:{sid}')
    with world.store._connect() as conn:
        conn.execute('''CREATE TRIGGER fail_replacement_link BEFORE UPDATE OF assistant_message_id ON chat_turns
                        WHEN OLD.assistant_message_id IS NULL AND NEW.assistant_message_id IS NOT NULL
                        BEGIN SELECT RAISE(ABORT, 'synthetic ledger failure'); END''')
    engine.llm.chunks = ['Replacement that cannot be committed']
    result = decoded(reliable.post('/api/chat/regenerate', json={
        'session_id': sid, 'message_id': mid, 'request_id': 'atomic-replacement-123'}))
    assert result[-1]['status'] == 'failed'
    assert world.store.get_message(mid).content == 'Hello!'
    assert world.store.get_key(f'knowledge_dirty:{sid}') == before_dirty


def test_regenerating_an_earlier_answer_excludes_later_messages_and_preserves_them(reliable, engine, world):
    first = first_reply(reliable)
    sid = first['session_id']
    later_user = world.store.save_message(sid, 'user', 'Later question')
    later_reply = world.store.save_message(sid, 'assistant', 'Later reply')
    engine.llm.chunks = ['New first answer']
    result = decoded(reliable.post('/api/chat/regenerate', json={
        'session_id': sid, 'message_id': first['message_id'], 'request_id': 'earlier-replacement-123'}))
    assert result[-1]['status'] == 'saved'
    assert world.store.get_message(later_user).content == 'Later question'
    assert world.store.get_message(later_reply).content == 'Later reply'
    assert [m['content'] for m in engine.llm.calls[-1][1:]] == ['Hello']


def test_greeting_can_be_regenerated_without_inventing_a_user_message(reliable, engine, world):
    sid = world.store.create_session('greeting')
    mid = world.store.save_message(sid, 'assistant', 'First words')
    engine.llm.chunks = ['Different first words']
    result = decoded(reliable.post('/api/chat/regenerate', json={
        'session_id': sid, 'message_id': mid, 'request_id': 'greeting-replacement-123'}))
    assert result[-1]['status'] == 'saved'
    assert [m.role for m in world.store.get_messages(sid)] == ['assistant']


def test_stop_retains_partial_and_retry_reuses_the_original_user_message(reliable, engine, world):
    blocked, release = threading.Event(), threading.Event()
    def pieces():
        yield 'Partial answer'
        blocked.set()
        assert release.wait(5)
        yield ' more text'
    engine.llm.chunks = pieces()
    original = reliable.post('/api/chat/stream', json={'message': 'Hello', 'request_id': 'stopped-request-123'}, buffered=False)
    try:
        assert blocked.wait(5)
        response = reliable.post('/api/chat/requests/stopped-request-123/stop')
        assert response.json['status'] == 'stopping'
    finally:
        release.set()
        original.close()
    result = list(get_turn_service().events('stopped-request-123'))[-1]
    assert result['status'] == 'interrupted' and result['response'] == 'Partial answer'
    assert [m.role for m in world.store.get_messages(result['session_id'])] == ['user']
    engine.llm.chunks = ['Finished answer']
    retry = decoded(reliable.post('/api/chat/regenerate', json={
        'session_id': result['session_id'], 'message_id': result['user_message_id'], 'request_id': 'retry-stopped-123'}))
    assert retry[-1]['status'] == 'saved'
    assert [m.content for m in world.store.get_messages(result['session_id'])] == ['Hello', 'Finished answer']


def test_stop_after_save_is_idempotent_and_cannot_remove_a_completed_reply(reliable, world):
    first = first_reply(reliable)
    assert reliable.post('/api/chat/requests/original-request-123/stop').json['status'] == 'saved'
    assert reliable.post('/api/chat/requests/does-not-exist-123/stop').status_code == 404
    assert world.store.get_message(first['message_id']).content == 'Hello!'


def test_failure_after_reply_commit_still_reports_the_saved_answer(reliable, engine, world, monkeypatch):
    from core.expression import get_expression
    original = engine._persist_reply
    def save_then_fail(session_id, content):
        original(session_id, content)
        raise OSError('synthetic failure after reply commit')
    monkeypatch.setattr(engine, '_persist_reply', save_then_fail)
    engine.llm.chunks = ["I'm so happy to see you."]
    result = first_reply(reliable)
    assert result['status'] == 'saved' and result['message_id']
    assert world.store.get_message(result['message_id']).content == "I'm so happy to see you."
    assert len(world.store.get_messages(result['session_id'])) == 2
    assert get_expression(world.store, '')['state'] == 'happy'


def test_regeneration_requires_a_message_in_the_selected_conversation(reliable, world):
    first = first_reply(reliable)
    other = world.store.create_session('other')
    assert reliable.post('/api/chat/regenerate', json={'session_id': other, 'message_id': first['message_id']}).status_code == 404
    assert reliable.post('/api/chat/regenerate', json={'session_id': [], 'message_id': 1}).status_code == 400


def test_edit_is_scoped_atomic_and_clears_old_replay_copies(reliable, world):
    first = first_reply(reliable, 'Original fact')
    sid, mid = first['session_id'], first['user_message_id']
    other = world.store.create_session('Other')
    assert reliable.patch(f'/api/messages/{mid}', json={'session_id': other, 'content': 'Wrong chat'}).status_code == 404
    assert reliable.patch(f'/api/messages/{mid}', json={'session_id': sid, 'content': ''}).status_code == 400
    response = reliable.patch(f'/api/messages/{mid}', json={'session_id': sid, 'content': 'Corrected fact'})
    assert response.status_code == 200 and response.json['knowledge_rebuild_pending']
    assert world.store.get_message(mid).content == 'Corrected fact'
    assert world.store.get_message(first['message_id']).content == 'Hello!'
    assert get_turn_service().get('original-request-123') is None
    assert reliable.post('/api/chat/stream', json={'message': 'Original fact', 'request_id': 'original-request-123'}).status_code == 410
    assert world.store.get_key(f'knowledge_dirty:{sid}')['pending'] is True
    with get_turn_service().connection() as conn:
        assert conn.execute('SELECT count(*) FROM chat_turn_events').fetchone()[0] == 0


def test_delete_removes_selected_message_and_replay_text_but_keeps_later_messages(reliable, world):
    first = first_reply(reliable)
    sid, mid = first['session_id'], first['message_id']
    later = world.store.save_message(sid, 'user', 'Later message')
    assert reliable.delete(f'/api/messages/{mid}', json={'session_id': sid}).status_code == 200
    assert world.store.get_message(mid) is None
    assert world.store.get_message(later).content == 'Later message'
    assert reliable.get(f'/api/messages/item/{mid}').status_code == 404
    assert get_turn_service().get('original-request-123') is None
    assert reliable.post('/api/chat/stream', json={'message': 'Hello', 'request_id': 'original-request-123'}).status_code == 410


def test_reviewed_edit_refuses_stale_text_owner_and_dataset(reliable, world):
    first = first_reply(reliable, 'Original fact')
    sid, mid = first['session_id'], first['user_message_id']
    epoch = world.store.dataset_epoch()['value']
    body = {'session_id': sid, 'content': 'Edited fact', 'expected_content': 'Original fact',
            'dataset_epoch': epoch, 'avatar_id': ''}
    for changes in ({'expected_content': 'Old fact'}, {'avatar_id': 'another'}, {'dataset_epoch': 'old-epoch'}):
        assert reliable.patch(f'/api/messages/{mid}', json={**body, **changes}).status_code == 409
        assert world.store.get_message(mid).content == 'Original fact'
        assert get_turn_service().get('original-request-123') is not None
    assert reliable.patch(f'/api/messages/{mid}', json=body).status_code == 200
    assert world.store.get_message(mid).content == 'Edited fact'
    assert reliable.patch(f'/api/messages/{mid}', json=body).status_code == 409
    assert world.store.get_message(mid).content == 'Edited fact'


def test_message_mutation_is_refused_during_an_active_reply(reliable, engine, world):
    blocked, release = threading.Event(), threading.Event()
    def pieces():
        yield 'Partial'
        blocked.set()
        assert release.wait(5)
        yield 'done'
    engine.llm.chunks = pieces()
    response = reliable.post('/api/chat/stream', json={'message': 'Hello', 'request_id': 'mutating-request-123'}, buffered=False)
    try:
        assert blocked.wait(5)
        row = get_turn_service().get('mutating-request-123')
        url = f"/api/messages/{row['user_message_id']}"
        assert reliable.patch(url, json={'session_id': row['session_id'], 'content': 'Change'}).status_code == 409
        assert reliable.delete(url, json={'session_id': row['session_id']}).status_code == 409
    finally:
        release.set()
        response.close()
        list(get_turn_service().events('mutating-request-123'))


def test_successful_retry_hides_the_previous_failed_draft(reliable, engine, world):
    first = first_reply(reliable)
    def broken():
        yield 'Bad replacement'
        raise ConnectionError('offline')
    engine.llm.chunks = broken()
    payload = {'session_id': first['session_id'], 'message_id': first['message_id'], 'request_id': 'bad-replacement-123'}
    assert decoded(reliable.post('/api/chat/regenerate', json=payload))[-1]['status'] == 'failed'
    engine.llm.chunks = ['Working replacement']
    payload['request_id'] = 'good-replacement-123'
    assert decoded(reliable.post('/api/chat/regenerate', json=payload))[-1]['status'] == 'saved'
    assert not any(m.get('draft') for m in reliable.get(f"/api/messages/{first['session_id']}").json)


def test_edit_excludes_stale_knowledge_and_stops_using_the_old_compaction_cutoff(engine, world, monkeypatch):
    from core import notes, memory
    from test_companion_api import active_avatar
    avatar = active_avatar(world)
    sid = world.store.ensure_avatar_session(avatar['id'], 'Mara')
    user = world.store.save_message(sid, 'user', 'Old fact')
    reply = world.store.save_message(sid, 'assistant', 'Old reply')
    note_store = notes.get_notes_service().store
    note_store.write(avatar['id'], about_you='- Old fact', our_story='- Old story')
    note_store.update_state(avatar['id'], compacted_through=reply, noted_through=reply)
    monkeypatch.setattr(memory.memory_manager, 'get_relevant_memories', lambda *a, **kw: [{'content': 'Old fact'}])
    world.store.change_message(user, sid, content='Corrected fact')
    ctx = engine.build_context(sid)
    assert ctx.notes == {} and ctx.relevant_memories == []
    assert engine._history_for(sid, ctx)[0]['content'] == 'Corrected fact'


def test_stop_closes_the_owned_client_even_before_stream_headers_arrive(reliable, engine, world, monkeypatch):
    import openai
    from types import SimpleNamespace
    from core.chat import ChatEngine
    from core.llm import LLMClient
    entered, closed = threading.Event(), threading.Event()
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        entered.set()
        assert closed.wait(5)
        raise ConnectionError('The owned connection was closed')
    sdk = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)), close=closed.set)
    monkeypatch.setattr(openai, 'OpenAI', lambda **kw: sdk)
    model = LLMClient(base_url='http://fake.example/v1', api_key='fake', model='fake')
    monkeypatch.setattr(ChatEngine, 'llm_client', property(lambda self: model))
    response = reliable.post('/api/chat/stream', json={'message': 'Hi', 'request_id': 'before-headers-123'}, buffered=False)
    try:
        assert entered.wait(5)
        assert reliable.post('/api/chat/requests/before-headers-123/stop').json['status'] == 'stopping'
        assert closed.is_set()
    finally:
        closed.set()
        response.close()
    assert list(get_turn_service().events('before-headers-123'))[-1]['status'] == 'interrupted'
    assert len(calls) == 1
