"""Alternative replies: regenerating keeps the answer it replaces, and the person can move between them.

The message row keeps its id, because memories, voice notes, episodes and the reply ledger all point at that id; only its
content changes. Every answer it has had lives in message_versions. Editing or deleting a message by hand keeps no old
text: that is a correction, not another reply.
"""
import pytest

from test_companion_api import engine, tmp_store, world  # noqa: F401
from test_polish_reliability import decoded, reliable  # noqa: F401


def first_reply(client, message='Hello'):
    return decoded(client.post('/api/chat/stream', json={'message': message, 'request_id': 'original-request-123'}))[-1]


def regenerate(reliable, engine, session_id, message_id, text, request_id):
    engine.llm.chunks = [text]
    return decoded(reliable.post('/api/chat/regenerate', json={
        'session_id': session_id, 'message_id': message_id, 'request_id': request_id}))[-1]


def test_regenerating_twice_keeps_both_earlier_answers(reliable, engine, world):
    first = first_reply(reliable)
    sid, mid = first['session_id'], first['message_id']
    assert regenerate(reliable, engine, sid, mid, 'Second answer', 'replacement-request-123')['status'] == 'saved'
    assert regenerate(reliable, engine, sid, mid, 'Third answer', 'replacement-request-456')['status'] == 'saved'
    versions = world.store.message_versions(mid)
    assert [v['content'] for v in versions] == ['Hello!', 'Second answer', 'Third answer']
    assert [v['current'] for v in versions] == [False, False, True]
    # The row keeps its id and shows the newest answer.
    assert [(m.id, m.content) for m in world.store.get_messages(sid, limit=10) if m.role == 'assistant'] == [(mid, 'Third answer')]
    assert len({v['version_id'] for v in versions}) == 3


def test_showing_an_earlier_answer_replaces_the_text_and_keeps_every_answer(reliable, engine, world):
    first = first_reply(reliable)
    sid, mid = first['session_id'], first['message_id']
    regenerate(reliable, engine, sid, mid, 'Second answer', 'replacement-request-123')
    versions = world.store.message_versions(mid)
    result = world.store.show_message_version(mid, sid, versions[0]['version_id'])
    assert result['changed'] is True and result['content'] == 'Hello!'
    again = world.store.message_versions(mid)
    assert [v['content'] for v in again] == ['Hello!', 'Second answer']
    current = [v for v in again if v['current']]
    assert len(current) == 1 and current[0]['content'] == 'Hello!'
    assert current[0]['version_id'] == versions[0]['version_id']
    assert [m.content for m in world.store.get_messages(sid, limit=10) if m.role == 'assistant'] == ['Hello!']
    # Showing the version already on screen changes nothing.
    assert world.store.show_message_version(mid, sid, versions[0]['version_id'])['changed'] is False
    # And switching back works: nothing was lost.
    world.store.show_message_version(mid, sid, versions[1]['version_id'])
    assert [m.content for m in world.store.get_messages(sid, limit=10) if m.role == 'assistant'] == ['Second answer']
    assert [v['content'] for v in world.store.message_versions(mid)] == ['Hello!', 'Second answer']


def test_showing_a_version_marks_knowledge_dirty(reliable, engine, world):
    first = first_reply(reliable)
    sid, mid = first['session_id'], first['message_id']
    regenerate(reliable, engine, sid, mid, 'Second answer', 'replacement-request-123')
    versions = world.store.message_versions(mid)
    result = world.store.show_message_version(mid, sid, versions[0]['version_id'])
    assert result['knowledge_rebuild_pending'] is True
    assert (world.store.get_key(f'knowledge_dirty:{sid}') or {}).get('pending') is True


def test_showing_a_version_checks_the_expected_text_and_owner(reliable, engine, world):
    first = first_reply(reliable)
    sid, mid = first['session_id'], first['message_id']
    regenerate(reliable, engine, sid, mid, 'Second answer', 'replacement-request-123')
    versions = world.store.message_versions(mid)
    with pytest.raises(ValueError):
        world.store.show_message_version(mid, sid, versions[0]['version_id'], expected_content='A different answer')
    with pytest.raises(ValueError):
        world.store.show_message_version(mid, sid, versions[0]['version_id'], expected_owner='someone-else')
    with pytest.raises(ValueError):
        world.store.show_message_version(mid, sid, 999999)
    assert [m.content for m in world.store.get_messages(sid, limit=10) if m.role == 'assistant'] == ['Second answer']


def test_showing_a_version_is_refused_while_a_reply_is_streaming(reliable, engine, world):
    first = first_reply(reliable)
    sid, mid = first['session_id'], first['message_id']
    versions = world.store.message_versions(mid)
    conn = world.store._connect()
    try:
        with conn:
            conn.execute("INSERT INTO chat_turns (request_id,session_id,fingerprint,status,process_id) "
                         "VALUES ('streaming-request-123',?,'x','streaming','someone')", (sid,))
    finally:
        conn.close()
    with pytest.raises(ValueError):
        world.store.show_message_version(mid, sid, versions[0]['version_id'])
    assert [m.content for m in world.store.get_messages(sid, limit=10) if m.role == 'assistant'] == ['Hello!']


def test_editing_a_message_drops_its_earlier_answers(reliable, engine, world):
    first = first_reply(reliable)
    sid, mid = first['session_id'], first['message_id']
    regenerate(reliable, engine, sid, mid, 'Second answer', 'replacement-request-123')
    assert len(world.store.message_versions(mid)) == 2
    world.store.change_message(mid, sid, content='Hand-corrected answer')
    # A hand edit is a correction: only its own text is left, and it is that message's single version.
    versions = world.store.message_versions(mid)
    assert [(v['version_id'], v['content'], v['current']) for v in versions] == [(mid, 'Hand-corrected answer', True)]
    assert [m.content for m in world.store.get_messages(sid, limit=10) if m.role == 'assistant'] == ['Hand-corrected answer']


def test_deleting_a_message_removes_its_earlier_answers(reliable, engine, world):
    first = first_reply(reliable)
    sid, mid = first['session_id'], first['message_id']
    regenerate(reliable, engine, sid, mid, 'Second answer', 'replacement-request-123')
    world.store.change_message(mid, sid, delete=True)
    assert world.store.message_versions(mid) == []
    assert [m.content for m in world.store.get_messages(sid, limit=10)] == ['Hello']


def test_showing_an_earlier_answer_keeps_the_text_it_left(reliable, engine, world):
    """The answer that was on screen is not lost by switching away from it."""
    first = first_reply(reliable)
    sid, mid = first['session_id'], first['message_id']
    regenerate(reliable, engine, sid, mid, 'Second answer', 'replacement-request-123')
    before = {v['version_id']: v['content'] for v in world.store.message_versions(mid)}
    showing = next(version_id for version_id, version in
                   ((v['version_id'], v) for v in world.store.message_versions(mid)) if version['current'])
    other = next(version_id for version_id, version in
                 ((v['version_id'], v) for v in world.store.message_versions(mid)) if not version['current'])
    world.store.show_message_version(mid, sid, other)
    after = {v['version_id']: v['content'] for v in world.store.message_versions(mid)}
    assert set(before) <= set(after)
    # The answer that was showing before the switch is still there, under its own id.
    assert after[showing] == before[showing]


def test_deleting_a_conversation_removes_its_versions(reliable, engine, world):
    first = first_reply(reliable)
    sid, mid = first['session_id'], first['message_id']
    regenerate(reliable, engine, sid, mid, 'Second answer', 'replacement-request-123')
    world.store.delete_session(sid)
    conn = world.store._connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM message_versions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM message_version_current").fetchone()[0] == 0
    finally:
        conn.close()


def test_clearing_all_data_removes_the_versions(reliable, engine, world):
    first = first_reply(reliable)
    sid, mid = first['session_id'], first['message_id']
    regenerate(reliable, engine, sid, mid, 'Second answer', 'replacement-request-123')
    world.store.reset_all_user_data()
    conn = world.store._connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM message_versions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM message_version_current").fetchone()[0] == 0
    finally:
        conn.close()


def test_splitting_a_conversation_keeps_the_versions_with_their_messages(reliable, engine, world):
    first = first_reply(reliable)
    sid, mid = first['session_id'], first['message_id']
    regenerate(reliable, engine, sid, mid, 'Second answer', 'replacement-request-123')
    user = [m.id for m in world.store.get_messages(sid, limit=10) if m.role == 'user'][0]
    world.store.save_message(sid, 'user', 'One more question')
    world.store.save_message(sid, 'assistant', 'One more answer')
    new_id = world.store.split_session_at(sid, user)
    # The versions stayed on the message row, wherever it now lives.
    assert len(world.store.message_versions(mid)) == 2
    assert new_id != sid


def test_the_messages_route_reports_how_many_answers_a_message_has(reliable, engine, world):
    first = first_reply(reliable)
    sid, mid = first['session_id'], first['message_id']
    before = reliable.get(f'/api/messages/{sid}').get_json()
    assert all(m['versions'] == 1 and m['other_versions'] == 0 for m in before)
    regenerate(reliable, engine, sid, mid, 'Second answer', 'replacement-request-123')
    after = reliable.get(f'/api/messages/{sid}').get_json()
    assert [(m['id'], m['versions'], m['other_versions']) for m in after if m['role'] == 'assistant'] == [(mid, 2, 1)]


def test_the_versions_routes_read_and_show(reliable, engine, world):
    first = first_reply(reliable)
    sid, mid = first['session_id'], first['message_id']
    regenerate(reliable, engine, sid, mid, 'Second answer', 'replacement-request-123')
    listed = reliable.get(f'/api/messages/{mid}/versions').get_json()
    assert listed['message_id'] == mid
    assert [v['content'] for v in listed['versions']] == ['Hello!', 'Second answer']
    chosen = listed['versions'][0]
    shown = reliable.post(f"/api/messages/{mid}/versions/{chosen['version_id']}",
                          json={'session_id': sid}).get_json()
    assert shown['status'] == 'ok' and shown['content'] == 'Hello!'
    # A version that does not belong to this message is refused.
    assert reliable.post(f'/api/messages/{mid}/versions/999999', json={'session_id': sid}).status_code == 409
    # A message that does not exist has no versions to read.
    assert reliable.get('/api/messages/999999/versions').status_code == 404


def test_a_failed_regeneration_keeps_the_answer_and_adds_no_version(reliable, engine, world):
    first = first_reply(reliable)
    sid, mid = first['session_id'], first['message_id']
    def broken():
        yield 'Unfinished replacement'
        raise ConnectionError('offline')
    engine.llm.chunks = broken()
    decoded(reliable.post('/api/chat/regenerate', json={
        'session_id': sid, 'message_id': mid, 'request_id': 'failed-replacement-123'}))
    assert [v['content'] for v in world.store.message_versions(mid)] == ['Hello!']
    assert [m.content for m in world.store.get_messages(sid, limit=10) if m.role == 'assistant'] == ['Hello!']


def test_the_schema_has_the_two_version_tables(tmp_store):
    tables = {row[0] for row in tmp_store._connect().execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert 'message_versions' in tables and 'message_version_current' in tables

def test_one_messages_versions_never_take_anothers(tmp_path, monkeypatch):
    """Version ids belong to their message. With one key shared by every message, regenerating a later answer overwrote
    an earlier answer's saved version, and switching back showed one chat's words in another person's chat."""
    from core.datastore import DataStore
    monkeypatch.setenv('ALTERA_HOME', str(tmp_path))
    ds = DataStore(str(tmp_path / 'versions.db'))
    a, b = ds.create_session('companion A'), ds.create_session('companion B')
    ds.save_message(a, 'user', 'hi')
    answer_a = ds.save_message(a, 'assistant', 'A first answer')
    ds.replace_reply(answer_a, a, 'A second answer')
    ds.replace_reply(answer_a, a, 'A third answer')
    ds.save_message(b, 'user', 'hello')
    answer_b = ds.save_message(b, 'assistant', 'B first answer')
    ds.replace_reply(answer_b, b, 'B second answer')
    ds.show_message_version(answer_a, a, answer_a)
    assert [v['content'] for v in ds.message_versions(answer_a)] == ['A first answer', 'A second answer', 'A third answer']
    assert [v['content'] for v in ds.message_versions(answer_b)] == ['B first answer', 'B second answer']
    for version in ds.message_versions(answer_a):
        assert ds.show_message_version(answer_a, a, version['version_id'])['content'].startswith('A ')
    for version in ds.message_versions(answer_b):
        assert ds.show_message_version(answer_b, b, version['version_id'])['content'].startswith('B ')


def test_a_database_with_the_shared_version_key_moves_to_the_per_message_key(tmp_path, monkeypatch):
    import sqlite3

    from core.datastore import DataStore, init_db
    monkeypatch.setenv('ALTERA_HOME', str(tmp_path))
    path = str(tmp_path / 'old.db')
    ds = DataStore(path)
    sid = ds.create_session('companion')
    ds.save_message(sid, 'user', 'hi')
    answer = ds.save_message(sid, 'assistant', 'Second answer')
    conn = sqlite3.connect(path)
    conn.executescript(f"""
        DROP TABLE message_version_current; DROP TABLE message_versions;
        CREATE TABLE message_versions (message_id INTEGER NOT NULL REFERENCES messages(id), version_id INTEGER PRIMARY KEY,
                                       content TEXT NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
        CREATE INDEX idx_message_versions ON message_versions(message_id, version_id);
        CREATE TABLE message_version_current (message_id INTEGER PRIMARY KEY REFERENCES messages(id),
                                              version_id INTEGER NOT NULL REFERENCES message_versions(version_id));
        INSERT INTO message_versions(message_id, version_id, content) VALUES ({answer}, {answer}, 'First answer'),
                                                                             ({answer}, {answer + 1}, 'Second answer');
        INSERT INTO message_version_current(message_id, version_id) VALUES ({answer}, {answer + 1});
    """)
    conn.close()
    init_db(path)
    conn = sqlite3.connect(path)
    keys = [row[1] for row in conn.execute('PRAGMA table_info(message_versions)') if row[5]]
    conn.close()
    assert keys == ['message_id', 'version_id']
    versions = DataStore(path).message_versions(answer)
    assert [(v['content'], v['current']) for v in versions] == [('First answer', False), ('Second answer', True)]
