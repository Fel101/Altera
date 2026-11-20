"""Episode evidence, curation and accounting use only an isolated fake store."""
from datetime import datetime, timedelta, UTC
import hashlib
import json
from types import SimpleNamespace as NS
import threading

from flask import Flask
import pytest

from core.datastore import DataStore
from core.episodes import (EpisodeError, EpisodeService, clear_all, delete_owner, ensure_schema,
                           invalidate_all, invalidate_owner, invalidate_session)
from core.knowledge import reference

NOW = datetime(2026, 10, 28, 16, tzinfo=UTC)


@pytest.fixture
def episode_world(tmp_path):
    ds = DataStore(str(tmp_path / 'episodes.db'))
    sid = ds.create_session('Synthetic episodes')
    user = ds.save_message(sid, 'user', 'I enjoyed jasmine tea by the window. 🌿')
    assistant = ds.save_message(sid, 'assistant', 'Maybe you grew up in Paris? That is only my guess.')
    epoch = ds.dataset_epoch()['value']
    ds._voice_note_service = NS(invalidate_owner=lambda *a, **kw: None)
    clock = [NOW]
    service = EpisodeService(ds, clock=lambda: clock[0])
    return NS(ds=ds, sid=sid, user=user, assistant=assistant, epoch=epoch, service=service, clock=clock)


def capture(w, **changes):
    data = {'request_id': 'synthetic_capture', 'kind': 'actual_exchange', 'title': 'Tea at the window',
            'sources': [{'message_id': w.user}, {'message_id': w.assistant}]}
    data.update(changes)
    return w.service.capture(w.sid, w.epoch, data)['episode']


def exchange(w, episodes=None):
    user = w.ds.save_message(w.sid, 'user', 'How about that jasmine tea?')
    assistant = w.ds.save_message(w.sid, 'assistant', 'A saved synthetic reply.')
    return w.service.record_recall(w.sid, w.epoch, episodes or [], user, assistant)


def test_capture_is_deterministic_attributed_and_does_not_extract_biography(episode_world):
    w = episode_world
    item = capture(w)
    assert item['kind'] == 'actual_exchange' and item['summary'] == ''
    assert item['confidence'] is None and item['curation'] == 'manual_saved'
    assert [s['role'] for s in item['sources']] == ['user', 'assistant']
    assert item['sources'][0]['excerpt'] == w.ds.get_message(w.user).content
    assert item['sources'][0]['sha256'] == reference(w.ds.get_message(w.user))['sha256']
    selected = w.service.preview_recall(w.sid, 'jasmine tea Paris')['episodes']
    assert len(selected) == 1
    assert 'assistant message' in selected[0]['text'] and 'only my guess' in selected[0]['text']
    assert 'not verified biography' in selected[0]['text']


def test_reads_and_capture_never_call_a_model_and_reads_do_not_write(episode_world, monkeypatch):
    w = episode_world
    from core import llm
    monkeypatch.setattr(llm, 'get_client', lambda *a, **kw: pytest.fail('Episode inspection cannot initialize a model'))
    capture(w)
    statements = []
    connect = w.ds._connect
    def observed():
        conn = connect()
        conn.set_trace_callback(statements.append)
        return conn
    monkeypatch.setattr(w.ds, '_connect', observed)
    for _ in range(3):
        assert w.service.inventory(w.sid)['episodes']
        assert w.service.preview_recall(w.sid, 'jasmine')['episodes']
    assert not [s for s in statements if s.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE', 'CREATE', 'ALTER'))]
    assert w.ds.get_message_count(w.sid) == 2


def test_capture_is_atomic_idempotent_and_conflicting_retry_is_rejected(episode_world):
    w = episode_world
    first = capture(w)
    assert capture(w)['id'] == first['id']
    with pytest.raises(EpisodeError, match='different episode'):
        capture(w, title='Different retry')
    with pytest.raises(EpisodeError):
        capture(w, request_id='invalid_sources', sources=[{'message_id': w.user}, {'message_id': 99999}])
    with w.ds._connect() as conn:
        assert conn.execute('SELECT count(*) FROM companion_episodes').fetchone()[0] == 1


def test_competing_capture_writers_create_one_episode(episode_world):
    w = episode_world
    results, errors = [], []
    def save():
        try:
            results.append(capture(w)['id'])
        except Exception as error:
            errors.append(error)
    workers = [threading.Thread(target=save) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(10)
    assert not errors and len(results) == 2 and len(set(results)) == 1


@pytest.mark.parametrize('mutation', ['edit', 'delete', 'floor', 'epoch', 'revision', 'dirty', 'owner'])
def test_stale_evidence_never_recalled_and_stale_content_is_suppressed(episode_world, mutation):
    w = episode_world
    item = capture(w)
    if mutation == 'edit':
        with w.ds._connect() as conn:
            conn.execute('UPDATE messages SET content=? WHERE id=?', ('Changed source', w.user))
            conn.commit()
    elif mutation == 'delete':
        with w.ds._connect() as conn:
            conn.execute('DELETE FROM messages WHERE id=?', (w.user,))
            conn.commit()
    elif mutation == 'floor':
        w.ds.set_key('knowledge_recall:@default', {'through': w.assistant})
    elif mutation == 'epoch':
        w.ds.set_key('dataset_epoch', {'value': 'changed_epoch'})
    elif mutation == 'revision':
        w.ds.set_key(f'knowledge_dirty:{w.sid}', {'revision': 1, 'pending': False})
    elif mutation == 'dirty':
        w.ds.set_key('knowledge_dirty_owner:@default', {'pending': True})
    else:
        other = w.ds.create_session('Other companion')
        with w.ds._connect() as conn:
            conn.execute("UPDATE sessions SET avatar_id='other_companion' WHERE id=?", (other,))
            conn.execute('UPDATE messages SET session_id=? WHERE id=?', (other, w.user))
            conn.commit()
    assert w.service.preview_recall(w.sid, 'tea Paris')['episodes'] == []
    rows = w.service.inventory(w.sid)['episodes']
    if mutation != 'epoch':
        assert rows[0]['id'] == item['id'] and rows[0]['status'] == 'stale'
        assert rows[0]['summary'] == '' and rows[0]['title'] == ''
    else:
        assert rows[0]['id'] == item['id'] and rows[0]['status'] == 'stale'
        assert not rows[0]['summary'] and not rows[0]['title']


def test_moved_source_ids_follow_current_same_owner_session_without_cross_owner_leak(episode_world):
    w = episode_world
    capture(w)
    destination = w.ds.create_session('Same owner destination')
    with w.ds._connect() as conn:
        conn.execute('UPDATE messages SET session_id=? WHERE id=?', (destination, w.user))
        conn.commit()
    item = w.service.inventory(destination)['episodes'][0]
    source = item['sources'][0]
    assert item['status'] == 'current'
    assert source['session_id'] == destination and source['original_session_id'] == w.sid
    with w.ds._connect() as conn:
        conn.execute("UPDATE sessions SET avatar_id='other' WHERE id=?", (destination,))
        conn.commit()
    assert w.service.inventory(w.sid)['episodes'][0]['status'] == 'stale'
    assert w.service.inventory(destination)['episodes'] == []


def test_manual_correction_and_pins_keep_confidence_separate_from_salience(episode_world):
    w = episode_world
    item = capture(w, kind='imagined_scene', summary='An imagined tea scene.', uncertainty='Fiction, not a real event.')
    fixed = w.service.change(item['id'], w.sid, w.epoch, item['revision'],
                            {'summary': 'Imagined rooftop tea.', 'pinned': True, 'salience': 5, 'in_story': True})['episode']
    assert fixed['revision'] == 2 and fixed['confidence'] is None
    assert fixed['curation'] == 'manual_corrected' and fixed['kind'] == 'imagined_scene'
    assert fixed['pinned'] and fixed['in_story'] and fixed['ranking']['pin_boost'] == .18
    recalled = w.service.preview_recall(w.sid, 'rooftop tea')['episodes'][0]
    assert 'never a real shared event' in recalled['text']
    with pytest.raises(EpisodeError, match='changed'):
        w.service.change(item['id'], w.sid, w.epoch, item['revision'], {'pinned': False})


def test_decay_lowers_priority_without_deleting_saved_or_pinned_episodes(episode_world):
    w = episode_world
    item = capture(w)
    initial = item['ranking']['score']
    w.clock[0] += timedelta(days=3000)
    later = w.service.inventory(w.sid)['episodes'][0]
    assert later['ranking']['score'] < initial and later['ranking']['recency'] == .2
    assert later['status'] == 'current' and later['id'] == item['id']


def test_forget_tombstone_is_retry_safe_and_cannot_be_recaptured_by_retry(episode_world):
    w = episode_world
    item = capture(w)
    forgotten = w.service.forget(item['id'], w.sid, w.epoch, item['revision'])['episode']
    assert forgotten['status'] == 'forgotten' and forgotten['sources'] == []
    assert w.service.forget(item['id'], w.sid, w.epoch, item['revision'])['episode']['status'] == 'forgotten'
    assert capture(w)['status'] == 'forgotten'
    assert w.service.inventory(w.sid)['episodes'] == []
    assert w.service.preview_recall(w.sid, 'tea')['episodes'] == []
    with w.ds._connect() as conn:
        row = conn.execute('SELECT title,summary,source_refs FROM companion_episodes').fetchone()
        assert tuple(row) == ('', '', '[]')


def test_preview_budget_is_read_only_and_spent_only_on_successful_actual_turn(episode_world):
    w = episode_world
    item = capture(w)
    for _ in range(3):
        assert w.service.preview_recall(w.sid, 'tea')['episodes']
    # A failed/nonexistent turn cannot reserve or deduct a recall.
    assert not w.service.record_recall(w.sid, w.epoch, [item['id']], 998, 999)
    assert w.service.preview_recall(w.sid, 'tea')['budget']['daily_used'] == 0
    assert exchange(w, [item['id']])
    assert not w.service.preview_recall(w.sid, 'tea')['budget']['available']
    for _ in range(4):
        assert exchange(w)
    assert w.service.preview_recall(w.sid, 'tea')['budget']['available']
    assert exchange(w, [item['id']])
    for _ in range(4):
        assert exchange(w)
    assert w.service.preview_recall(w.sid, 'tea')['budget']['daily_used'] == 2
    assert not w.service.preview_recall(w.sid, 'tea')['budget']['available']
    w.clock[0] += timedelta(days=1)
    assert w.service.preview_recall(w.sid, 'tea')['budget']['available']


def test_recall_accounting_rejects_unsaved_turns_and_is_idempotent(episode_world):
    w = episode_world
    item = capture(w)
    with w.ds._connect() as conn:
        conn.execute("INSERT INTO chat_turns(request_id,session_id,fingerprint,process_id,status,user_message_id,assistant_message_id) VALUES('fake_turn',?,'fake','fake','streaming',?,?)", (w.sid, w.user, w.assistant))
        conn.commit()
    assert not w.service.record_recall(w.sid, w.epoch, [item['id']], w.user, w.assistant)
    with w.ds._connect() as conn:
        conn.execute("UPDATE chat_turns SET status='saved'")
        conn.commit()
    assert w.service.record_recall(w.sid, w.epoch, [item['id']], w.user, w.assistant)
    assert w.service.record_recall(w.sid, w.epoch, [item['id']], w.user, w.assistant)
    with w.ds._connect() as conn:
        assert conn.execute('SELECT count(*) FROM episode_recall_turns').fetchone()[0] == 1


def test_reflection_publication_is_atomic_idempotent_and_needs_explicit_curation(episode_world):
    w = episode_world
    message = w.ds.get_message(w.user)
    proposal = {'title': 'Tea reflection', 'summary': 'The user mentioned enjoying tea.', 'uncertainty': 'One message only.',
                'confidence': .7, 'sources': [{**reference(message), 'role': 'user', 'start': 0, 'end': len(message.content)}]}
    with w.ds._connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        first = w.service.propose_reflection(conn, owner='', session_id=w.sid, epoch=w.epoch,
                                             run_id='fake_reflection', proposals=[proposal], now=NOW)[0]
        conn.commit()
    assert first['curation'] == 'reflection' and not first['eligible'] and first['confidence'] == .7
    assert not w.service.preview_recall(w.sid, 'tea')['episodes']
    fixed = w.service.change(first['id'], w.sid, w.epoch, first['revision'], {'in_story': True})['episode']
    assert fixed['confidence'] == .7 and fixed['eligible']
    with w.ds._connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        second = w.service.propose_reflection(conn, owner='', session_id=w.sid, epoch=w.epoch,
                                              run_id='fake_reflection', proposals=[{**proposal, 'summary': 'Fresh model wording cannot overwrite curation.'}], now=NOW)[0]
        conn.commit()
    assert second['id'] == first['id'] and second['in_story'] and second['curation'] == 'manual_curated'
    with w.ds._connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        with pytest.raises(EpisodeError):
            w.service.propose_reflection(conn, owner='', session_id=w.sid, epoch=w.epoch,
                run_id='rollback_run', proposals=[proposal, {**proposal, 'sources': [{**proposal['sources'][0], 'sha256': '0' * 64}]}])
        conn.rollback()
        assert conn.execute('SELECT count(*) FROM companion_episodes').fetchone()[0] == 1


def test_derived_inbox_and_assistant_reflections_are_never_original_evidence(episode_world):
    w = episode_world
    with w.ds._connect() as conn:
        conn.execute("INSERT INTO presence_inbox(id,job_id,avatar_id,session_id,dataset_epoch,kind,text,created_at,expires_at,snapshot,accepted_message_id) VALUES('fake_inbox','fake_job','',?,?,'check_in','Generated',1,9999999999,'{}',?)", (w.sid, w.epoch, w.assistant))
        conn.commit()
    with pytest.raises(EpisodeError, match='fresh episode evidence'):
        capture(w)
    proposal = {'title': 'Guess', 'summary': 'An assistant guess is not biography.',
                'sources': [{**reference(w.ds.get_message(w.assistant)), 'role': 'assistant', 'start': 0, 'end': 10}]}
    with w.ds._connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        with pytest.raises(EpisodeError, match='original user'):
            w.service.propose_reflection(conn, owner='', session_id=w.sid, epoch=w.epoch, run_id='bad_guess', proposals=[proposal])
        conn.rollback()


def test_schema_and_lifecycle_seams_preserve_callers_transaction(episode_world):
    w = episode_world
    item = capture(w)
    with w.ds._connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        ensure_schema(conn)
        invalidate_session(conn, w.sid)
        conn.rollback()
    assert w.service.inventory(w.sid)['episodes'][0]['status'] == 'current'
    with w.ds._connect() as conn:
        invalidate_owner(conn, '')
        conn.commit()
    assert w.service.inventory(w.sid)['episodes'][0]['status'] == 'stale'
    with w.ds._connect() as conn:
        invalidate_all(conn)
        delete_owner(conn, '')
        clear_all(conn)
        conn.commit()
    assert w.service.inventory(w.sid)['episodes'] == []
    assert item['id']


@pytest.mark.parametrize('changes', [
    {'kind': []}, {'kind': 'biography'}, {'title': '\ud800'}, {'title': 'x' * 161},
    {'salience': True}, {'salience': 6}, {'in_story': 1}, {'sources': []},
    {'sources': [{'message_id': True}]}, {'sources': [{'message_id': 1, 'start': -1}]},
    {'sources': [{'message_id': 1, 'start': 5, 'end': 3}]}, {'secret_extra': 'no'},
])
def test_capture_validation_rejects_malformed_fields(episode_world, changes):
    with pytest.raises(EpisodeError):
        capture(episode_world, **changes)


def test_epoch_owner_and_reviewed_hash_are_enforced_on_write(episode_world):
    w = episode_world
    with pytest.raises(EpisodeError, match='dataset_epoch'):
        w.service.capture(w.sid, None, {'request_id': 'valid_request'})
    with pytest.raises(EpisodeError, match='companion'):
        w.service.capture(w.sid, w.epoch, {'request_id': 'fake_request'}, owner='other')
    with pytest.raises(EpisodeError, match='source changed'):
        capture(w, sources=[{'message_id': w.user, 'sha256': '0' * 64}])
    sha = hashlib.sha256(w.ds.get_message(w.user).content.encode()).hexdigest()
    assert capture(w, sources=[{'message_id': w.user, 'sha256': sha}])['status'] == 'current'


def test_routes_bound_scope_fields_and_no_store_headers(episode_world, monkeypatch):
    w = episode_world
    import core.routes.episodes as routes
    app = Flask(__name__)
    app.register_blueprint(routes.bp)
    monkeypatch.setattr(routes, 'get_episode_service', lambda: w.service)
    client = app.test_client()
    for suffix in ('?session_id=true', '?session_id=0', '?session_id=1&session_id=2', '?session_id=1&extra=no'):
        assert client.get('/api/episodes' + suffix).status_code == 400
    result = client.get(f'/api/episodes?session_id={w.sid}')
    assert result.status_code == 200 and result.headers['Cache-Control'] == 'no-store'
    data = {'session_id': w.sid, 'dataset_epoch': w.epoch, 'avatar_id': '', 'request_id': 'http_request',
            'kind': 'imagined_scene', 'title': 'A fictional tea room', 'sources': [{'message_id': w.user}]}
    result = client.post('/api/episodes', json=data)
    assert result.status_code == 201
    item = result.get_json()['episode']
    missing_owner = {k: v for k, v in data.items() if k != 'avatar_id'}
    assert client.post('/api/episodes', json=missing_owner).status_code == 400
    assert client.post('/api/episodes', json={**data, 'avatar_id': 'wrong'}).status_code == 409
    assert client.post('/api/episodes', json={**data, 'unknown': 1}).status_code == 400
    assert client.post('/api/episodes', data='x' * 17000, content_type='application/json').status_code == 413
    for raw in ('{"avatar_id":"","avatar_id":"other"}', '{"avatar_id":"","salience":NaN}'):
        assert client.post('/api/episodes', data=raw, content_type='application/json').status_code == 400
    assert client.patch('/api/episodes/' + item['id'], json={'session_id': w.sid, 'dataset_epoch': w.epoch,
        'avatar_id': '', 'revision': item['revision'], 'pinned': True}).status_code == 200
    assert client.delete('/api/episodes/' + item['id'], json={'session_id': w.sid, 'dataset_epoch': w.epoch,
        'avatar_id': '', 'revision': 2, 'confirm': 'forget'}).get_json()['episode']['status'] == 'forgotten'


def active_turn(w, request_id, sid=None):
    from core.turns import PROCESS_ID
    sid = sid or w.sid
    user = w.ds.save_message(sid, 'user', 'Let us discuss jasmine tea.')
    with w.ds._connect() as conn:
        conn.execute('INSERT INTO chat_turns(request_id,session_id,fingerprint,process_id,status,user_message_id) VALUES(?,?,?,?,?,?)',
                     (request_id, sid, 'synthetic', PROCESS_ID, 'sending', user))
        conn.commit()
    return user


def finish_turn(w, request_id, user, ids, sid=None):
    sid = sid or w.sid
    assistant = w.ds.save_message(sid, 'assistant', 'A completed synthetic reply.')
    with w.ds._connect() as conn:
        conn.execute("UPDATE chat_turns SET status='saved',assistant_message_id=? WHERE request_id=?", (assistant, request_id))
        conn.commit()
    return w.service.record_recall(sid, w.epoch, ids, user, assistant, request_id=request_id)


def test_pending_reservation_blocks_second_session_without_spending_budget(episode_world):
    w = episode_world
    episode = capture(w)
    other = w.ds.create_session('Concurrent same-owner session')
    first_user = active_turn(w, 'first_pending')
    second_user = active_turn(w, 'second_pending', other)
    first = w.service.select_for_turn(w.sid, w.epoch, 'first_pending', 'jasmine tea')
    assert [item['id'] for item in first] == [episode['id']]
    assert w.service.select_for_turn(other, w.epoch, 'second_pending', 'jasmine tea') == []
    assert w.service.preview_recall(other, 'jasmine tea')['budget']['daily_used'] == 0
    assert w.service.preview_recall(other, 'jasmine tea')['episodes']  # Preview is independent and read only.
    # Normal completion cannot steal a pending turn's budget slot.
    assert finish_turn(w, 'second_pending', second_user, [], other)
    assert finish_turn(w, 'first_pending', first_user, [episode['id']])
    assert w.service.preview_recall(other, 'jasmine tea')['budget']['daily_used'] == 1
    with w.ds._connect() as conn:
        assert conn.execute('SELECT count(*) FROM episode_recall_reservations').fetchone()[0] == 0


def test_failed_turn_release_preserves_budget_and_allows_other_session(episode_world):
    w = episode_world
    capture(w)
    other = w.ds.create_session('Second session')
    active_turn(w, 'failed_pending')
    active_turn(w, 'waiting_pending', other)
    assert w.service.select_for_turn(w.sid, w.epoch, 'failed_pending', 'tea')
    w.service.release_recall('failed_pending')
    w.service.release_recall('failed_pending')  # Finally cleanup is idempotent.
    assert w.service.select_for_turn(other, w.epoch, 'waiting_pending', 'tea')
    assert w.service.preview_recall(other, 'tea')['budget']['daily_used'] == 0


def test_source_invalidation_cancels_pending_recall_and_never_deducts_budget(episode_world):
    w = episode_world
    episode = capture(w)
    user = active_turn(w, 'source_change_pending')
    assert w.service.select_for_turn(w.sid, w.epoch, 'source_change_pending', 'tea')
    with w.ds._connect() as conn:
        conn.execute('UPDATE messages SET content=? WHERE id=?', ('Corrected source', w.user))
        invalidate_session(conn, w.sid)
        conn.commit()
    assert not finish_turn(w, 'source_change_pending', user, [episode['id']])
    assert w.service.preview_recall(w.sid, 'tea')['budget']['daily_used'] == 0


@pytest.mark.parametrize('mutation', ['process', 'finished', 'missing', 'epoch', 'owner'])
def test_reservation_recovery_uses_durable_identity_not_wall_clock(episode_world, mutation):
    w = episode_world
    capture(w)
    active_turn(w, 'recover_pending')
    assert w.service.select_for_turn(w.sid, w.epoch, 'recover_pending', 'tea')
    # Same-process reinitialization must preserve a live reservation indefinitely.
    EpisodeService(w.ds, clock=lambda: NOW + timedelta(days=3000))
    with w.ds._connect() as conn:
        assert conn.execute('SELECT count(*) FROM episode_recall_reservations').fetchone()[0] == 1
        if mutation == 'process':
            conn.execute("UPDATE chat_turns SET process_id='retired_process'")
        elif mutation == 'finished':
            conn.execute("UPDATE chat_turns SET status='failed'")
        elif mutation == 'missing':
            conn.execute('DELETE FROM chat_turns')
        elif mutation == 'epoch':
            conn.execute("UPDATE user_profile SET value='{\"value\":\"restored\"}' WHERE key='dataset_epoch'")
        else:
            conn.execute("UPDATE sessions SET avatar_id='different_companion' WHERE id=?", (w.sid,))
        conn.commit()
    EpisodeService(w.ds, clock=lambda: NOW)
    with w.ds._connect() as conn:
        assert conn.execute('SELECT count(*) FROM episode_recall_reservations').fetchone()[0] == 0


@pytest.mark.parametrize('change', ['none', 'source', 'forgotten', 'owner', 'epoch', 'process_mismatch',
                                    'invalid_time', 'future_time', 'bad_ids', 'missing_user', 'foreign_answer'])
def test_restart_accounts_only_a_saved_unchanged_reserved_recall(episode_world, change):
    from core.turns import TurnService
    w = episode_world
    episode = capture(w)
    user = active_turn(w, 'restart_recall')
    assert [item['id'] for item in w.service.select_for_turn(w.sid, w.epoch, 'restart_recall', 'tea')] == [episode['id']]
    answer_session = w.ds.create_session('Other') if change == 'foreign_answer' else w.sid
    answer = w.ds.save_message(answer_session, 'assistant', 'A completed synthetic reply.')
    timestamp = {'invalid_time': 'broken', 'future_time': '2030-01-01 00:00:00'}.get(change, '2026-10-28 15:00:00')
    with w.ds._connect() as conn:
        conn.execute("UPDATE chat_turns SET process_id='old-process',assistant_message_id=?,updated_at=? WHERE request_id=?",
                     (answer, timestamp, 'restart_recall'))
        conn.execute("UPDATE episode_recall_reservations SET process_id='old-process' WHERE request_id='restart_recall'")
        if change == 'source':
            conn.execute('UPDATE messages SET content=? WHERE id=?', ('A changed source', w.user))
        elif change == 'forgotten':
            conn.execute("UPDATE companion_episodes SET status='forgotten' WHERE id=?", (episode['id'],))
        elif change == 'owner':
            conn.execute("UPDATE sessions SET avatar_id='other-companion' WHERE id=?", (w.sid,))
        elif change == 'epoch':
            conn.execute("UPDATE user_profile SET value=? WHERE key='dataset_epoch'", (json.dumps({'value': 'restored'}),))
        elif change == 'process_mismatch':
            conn.execute("UPDATE episode_recall_reservations SET process_id='different-process' WHERE request_id='restart_recall'")
        elif change == 'bad_ids':
            conn.execute("UPDATE episode_recall_reservations SET episode_ids='not-json' WHERE request_id='restart_recall'")
        elif change == 'missing_user':
            conn.execute("UPDATE chat_turns SET user_message_id=NULL WHERE request_id='restart_recall'")
    TurnService(w.ds)  # Normal startup reconciles turn status before episode recovery.
    EpisodeService(w.ds, clock=lambda: NOW)
    EpisodeService(w.ds, clock=lambda: NOW)  # Repeated startup must not spend twice.
    with w.ds._connect() as conn:
        rows = conn.execute('SELECT episode_ids,used_at FROM episode_recall_turns WHERE user_message_id=?', (user,)).fetchall()
        assert conn.execute('SELECT count(*) FROM episode_recall_reservations').fetchone()[0] == 0
    if change == 'none':
        assert len(rows) == 1 and json.loads(rows[0]['episode_ids']) == [episode['id']]
        assert rows[0]['used_at'] == '2026-10-28T15:00:00+00:00'
        assert w.service.preview_recall(w.sid, 'tea')['budget']['daily_used'] == 1
    else:
        assert not rows or all(json.loads(row['episode_ids']) == [] for row in rows)
        if change not in ('owner', 'epoch'):
            assert w.service.preview_recall(w.sid, 'tea')['budget']['daily_used'] == 0


def test_restart_accounting_and_reservation_removal_roll_back_together(episode_world):
    from core.turns import TurnService
    import sqlite3
    w = episode_world
    episode = capture(w)
    user = active_turn(w, 'restart_recall_atomic')
    assert w.service.select_for_turn(w.sid, w.epoch, 'restart_recall_atomic', 'tea')
    answer = w.ds.save_message(w.sid, 'assistant', 'A completed synthetic reply.')
    with w.ds._connect() as conn:
        conn.execute("UPDATE chat_turns SET process_id='old-process',assistant_message_id=?,updated_at='2026-10-28 15:00:00' WHERE request_id='restart_recall_atomic'", (answer,))
        conn.execute("UPDATE episode_recall_reservations SET process_id='old-process' WHERE request_id='restart_recall_atomic'")
        conn.execute("""CREATE TRIGGER fail_recall_cleanup BEFORE DELETE ON episode_recall_reservations
                        BEGIN SELECT RAISE(ABORT, 'synthetic cleanup failure'); END""")
    TurnService(w.ds)
    with pytest.raises(sqlite3.IntegrityError):
        EpisodeService(w.ds, clock=lambda: NOW)
    with w.ds._connect() as conn:
        assert conn.execute('SELECT count(*) FROM episode_recall_turns WHERE user_message_id=?', (user,)).fetchone()[0] == 0
        assert conn.execute('SELECT count(*) FROM episode_recall_reservations').fetchone()[0] == 1
        conn.execute('DROP TRIGGER fail_recall_cleanup')
    EpisodeService(w.ds, clock=lambda: NOW)
    with w.ds._connect() as conn:
        row = conn.execute('SELECT episode_ids FROM episode_recall_turns WHERE user_message_id=?', (user,)).fetchone()
        assert json.loads(row['episode_ids']) == [episode['id']]
        assert conn.execute('SELECT count(*) FROM episode_recall_reservations').fetchone()[0] == 0


def test_selection_requires_actual_current_process_turn_and_current_epoch(episode_world):
    w = episode_world
    capture(w)
    assert w.service.select_for_turn(w.sid, w.epoch, 'nonexistent_request', 'tea') == []
    active_turn(w, 'real_pending')
    with pytest.raises(EpisodeError, match='dataset changed'):
        w.service.select_for_turn(w.sid, 'restored_epoch', 'real_pending', 'tea')
    with w.ds._connect() as conn:
        conn.execute("UPDATE chat_turns SET process_id='old_process'")
        conn.commit()
    assert w.service.select_for_turn(w.sid, w.epoch, 'real_pending', 'tea') == []


def test_forget_advances_boundary_once_and_original_sources_cannot_recreate(episode_world):
    w = episode_world
    item = capture(w)
    result = w.service.forget(item['id'], w.sid, w.epoch, item['revision'])
    assert result['recall_after'] == w.assistant
    boundary = w.ds.get_key('knowledge_recall:@default')
    later = w.ds.save_message(w.sid, 'user', 'Fresh later conversation.')
    assert w.service.forget(item['id'], w.sid, w.epoch, item['revision'])['episode']['status'] == 'forgotten'
    assert w.ds.get_key('knowledge_recall:@default') == boundary
    with pytest.raises(EpisodeError, match='recall boundary'):
        capture(w, request_id='new_capture_for_old_sources')
    assert later > boundary['through']
    assert not w.ds.get_key('knowledge_dirty_owner:@default')['pending']


def test_forget_keeps_preexisting_knowledge_repair_paused(episode_world):
    w = episode_world
    item = capture(w)
    w.ds.set_key('knowledge_dirty_owner:@default', {'pending': True})
    result = w.service.forget(item['id'], w.sid, w.epoch, item['revision'])
    assert result['episode']['status'] == 'forgotten'
    assert w.ds.get_key('knowledge_dirty_owner:@default')['pending'] is True


def test_invalid_forget_revision_has_no_boundary_or_notes_side_effect(episode_world):
    w = episode_world
    item = capture(w)
    w.service.knowledge_factory = lambda store: pytest.fail('Preflight must reject before accessing knowledge services')
    with pytest.raises(EpisodeError, match='Episode changed'):
        w.service.forget(item['id'], w.sid, w.epoch, item['revision'] + 1)
    assert w.ds.get_key('knowledge_recall:@default') is None
    assert w.ds.get_key('knowledge_dirty_owner:@default') is None
    assert w.service.inventory(w.sid)['episodes'][0]['status'] == 'current'


def test_forget_boundary_failure_leaves_pending_knowledge_fail_closed(episode_world, monkeypatch):
    from core.knowledge import KnowledgeService
    w = episode_world
    item = capture(w)
    knowledge = KnowledgeService(w.ds, manager=None)
    cleared = []
    monkeypatch.setattr(knowledge, '_clear_notes', lambda owner: cleared.append(owner))
    def fail_boundary(owner):
        raise OSError('Synthetic boundary persistence failure')
    monkeypatch.setattr(knowledge, '_boundary', fail_boundary)
    w.service.knowledge_factory = lambda store: knowledge
    with pytest.raises(OSError, match='Synthetic boundary'):
        w.service.forget(item['id'], w.sid, w.epoch, item['revision'])
    assert cleared == [''] and w.ds.get_key('knowledge_dirty_owner:@default')['pending']
    assert w.service.preview_recall(w.sid, 'tea')['episodes'] == []
    with w.ds._connect() as conn:
        assert conn.execute('SELECT status FROM companion_episodes').fetchone()[0] == 'current'
    assert w.service.inventory(w.sid)['episodes'][0]['status'] == 'stale'


@pytest.mark.parametrize(('field', 'invalid'), [
    ('summary', 'underage sex'), ('summary', 'x' * 1201), ('title', 'x' * 161),
    ('uncertainty', 'x' * 401), ('kind', 'verified_biography'), ('curation', 'verified_fact'),
    ('salience', 0), ('salience', 6), ('confidence', 1.5), ('confidence', float('inf')),
])
def test_restored_curation_is_validated_before_review_and_revalidation(episode_world, field, invalid):
    """Uploaded rows cannot bypass normal classification, size or safety limits."""
    w = episode_world
    original = capture(w)
    with w.ds._connect() as conn:
        conn.execute(f'UPDATE companion_episodes SET {field}=? WHERE id=?', (invalid, original['id']))
        refs = json.loads(conn.execute('SELECT source_refs FROM companion_episodes WHERE id=?', (original['id'],)).fetchone()[0])
        invalidate_all(conn)
        conn.commit()
    current_epoch = 'synthetic_restored_epoch'
    w.ds.set_key('dataset_epoch', {'value': current_epoch})
    assert w.service.preview_recall(w.sid, 'tea')['episodes'] == []
    with pytest.raises(EpisodeError):
        w.service.review_restored(original['id'], w.sid, current_epoch, original['revision'], owner='')
    # A client cannot skip the review endpoint and post the retained refs directly.
    with pytest.raises(EpisodeError):
        w.service.revalidate_restored(original['id'], w.sid, current_epoch, original['revision'], refs, owner='')
    with w.ds._connect() as conn:
        row = conn.execute('SELECT status,dataset_epoch,revision FROM companion_episodes WHERE id=?', (original['id'],)).fetchone()
        assert tuple(row) == ('stale', w.epoch, original['revision'])


def test_restored_capture_collision_is_a_clean_conflict_without_overwriting_newer_curation(episode_world):
    w = episode_world
    old = capture(w, summary='The retained older curation.')
    with w.ds._connect() as conn:
        invalidate_all(conn)
        conn.commit()
    current_epoch = 'synthetic_restored_epoch'
    w.ds.set_key('dataset_epoch', {'value': current_epoch})
    data = {'request_id': 'synthetic_capture', 'kind': 'actual_exchange', 'title': 'Tea at the window',
            'summary': 'The newly saved curation.', 'sources': [{'message_id': w.user}, {'message_id': w.assistant}]}
    newer = w.service.capture(w.sid, current_epoch, data)['episode']
    reviewed = w.service.review_restored(old['id'], w.sid, current_epoch, old['revision'], owner='')
    with pytest.raises(EpisodeError) as caught:
        w.service.revalidate_restored(old['id'], w.sid, current_epoch, old['revision'], reviewed['reviewed_sources'], owner='')
    assert caught.value.status == 409
    with w.ds._connect() as conn:
        records = {row['id']: dict(row) for row in conn.execute('SELECT id,dataset_epoch,status,summary,revision FROM companion_episodes')}
    assert records[old['id']]['dataset_epoch'] == w.epoch and records[old['id']]['status'] == 'stale'
    assert records[old['id']]['summary'] == 'The retained older curation.'
    assert records[newer['id']]['dataset_epoch'] == current_epoch and records[newer['id']]['status'] == 'current'
    assert records[newer['id']]['summary'] == 'The newly saved curation.' and records[newer['id']]['revision'] == 1


def test_reserved_retry_keeps_exact_episode_order_and_successful_usage_after_new_higher_ranked_captures(episode_world):
    w = episode_world
    low = capture(w, request_id='reserved_original_low', salience=1)
    high = capture(w, request_id='reserved_original_high', salience=2)
    user = active_turn(w, 'stable_retry_request')
    original = w.service.select_for_turn(w.sid, w.epoch, 'stable_retry_request', 'jasmine tea')
    expected_ids = [high['id'], low['id']]
    assert [item['id'] for item in original] == expected_ids
    newest = [capture(w, request_id='new_priority_one', salience=5),
              capture(w, request_id='new_priority_two', salience=5)]
    preview = w.service.preview_recall(w.sid, 'jasmine tea')
    assert {item['id'] for item in preview['episodes']} == {item['id'] for item in newest}
    assert preview['budget']['daily_used'] == 0
    # Retrying the same turn keeps its original evidence even if a later stage
    # supplies a different query or the global ranking changes meanwhile.
    retried = w.service.select_for_turn(w.sid, w.epoch, 'stable_retry_request', 'unrelated astronomy')
    assert [item['id'] for item in retried] == expected_ids
    assert [item['sources'] for item in retried] == [item['sources'] for item in original]
    assert [item['text'] for item in retried] == [item['text'] for item in original]
    assert finish_turn(w, 'stable_retry_request', user, expected_ids)
    assert w.service.preview_recall(w.sid, 'jasmine tea')['budget']['daily_used'] == 1
    with w.ds._connect() as conn:
        usage = conn.execute('SELECT episode_ids FROM episode_recall_turns WHERE user_message_id=?', (user,)).fetchone()
        assert json.loads(usage[0]) == expected_ids
        assert conn.execute('SELECT count(*) FROM episode_recall_reservations').fetchone()[0] == 0


def test_source_choices_are_newest_sixty_bounded_unicode_previews(episode_world):
    w = episode_world
    identifiers = []
    for index in range(65):
        identifiers.append(w.ds.save_message(w.sid, 'user' if index % 2 else 'assistant', '🌿' * 140 + str(index)))
    result = w.service.sources(w.sid)
    assert result['scope'] == {'session_id': w.sid, 'avatar_id': '', 'dataset_epoch': w.epoch}
    assert result['recall_after'] == 0 and result['repair_pending'] is False
    assert [item['message_id'] for item in result['sources']] == list(reversed(identifiers[-60:]))
    for item in result['sources']:
        assert set(item) == {'message_id', 'session_id', 'role', 'preview', 'timestamp'}
        assert item['session_id'] == w.sid and item['role'] in {'user', 'assistant'}
        assert item['preview'] == '🌿' * 120 and len(item['preview']) == 120
        assert isinstance(item['timestamp'], str)


def test_source_choices_exclude_other_sessions_foreign_owners_floor_derived_and_blocked_sources(episode_world):
    w = episode_world
    w.ds.set_key('knowledge_recall:@default', {'through': w.assistant})
    current_user = w.ds.save_message(w.sid, 'user', 'A current eligible user source.')
    current_assistant = w.ds.save_message(w.sid, 'assistant', 'A current eligible assistant source.')
    same_owner_other_session = w.ds.create_session('Different conversation, same companion')
    w.ds.save_message(same_owner_other_session, 'user', 'A separate conversation source.')
    foreign = w.ds.create_session('Other companion')
    w.ds.save_message(foreign, 'user', 'Private other companion source.')
    w.ds.save_message(w.sid, 'system', 'Internal instruction, never a source choice.')
    w.ds.save_message(w.sid, 'user', 'underage sex')
    generated = w.ds.save_message(w.sid, 'assistant', 'Generated inbox text, never original evidence.')
    with w.ds._connect() as conn:
        conn.execute("UPDATE sessions SET avatar_id='foreign_companion' WHERE id=?", (foreign,))
        conn.execute("INSERT INTO presence_inbox(id,job_id,avatar_id,session_id,dataset_epoch,kind,text,created_at,expires_at,snapshot,accepted_message_id) VALUES('source_picker_inbox','source_picker_job','',?,?,'check_in','Generated',1,9999999999,'{}',?)",
                     (w.sid, w.epoch, generated))
        conn.commit()
    result = w.service.sources(w.sid)
    assert result['recall_after'] == w.assistant
    assert [item['message_id'] for item in result['sources']] == [current_assistant, current_user]
    assert all('Private' not in item['preview'] and 'Generated' not in item['preview'] for item in result['sources'])


@pytest.mark.parametrize('dirty_scope', ['owner', 'session', 'other_owned_session'])
def test_source_choices_are_empty_while_companion_knowledge_needs_repair(episode_world, dirty_scope):
    w = episode_world
    if dirty_scope == 'owner':
        key = 'knowledge_dirty_owner:@default'
    elif dirty_scope == 'session':
        key = f'knowledge_dirty:{w.sid}'
    else:
        other = w.ds.create_session('Another owned conversation')
        key = f'knowledge_dirty:{other}'
    w.ds.set_key(key, {'pending': True, 'revision': 1})
    result = w.service.sources(w.sid)
    assert result['repair_pending'] is True and result['sources'] == []
    assert result['scope']['session_id'] == w.sid


def test_source_choices_service_and_api_are_read_only_strictly_scoped_and_do_not_infer(episode_world, monkeypatch):
    w = episode_world
    import core.routes.episodes as routes
    from core import llm
    monkeypatch.setattr(llm, 'get_client', lambda *a, **kw: pytest.fail('Source choices cannot initialize a model'))
    monkeypatch.setattr(routes, 'get_episode_service', lambda: w.service)
    app = Flask(__name__)
    app.register_blueprint(routes.bp)
    client = app.test_client()
    statements = []
    connect = w.ds._connect
    def observed():
        conn = connect()
        conn.set_trace_callback(statements.append)
        return conn
    monkeypatch.setattr(w.ds, '_connect', observed)
    for _ in range(2):
        assert len(w.service.sources(w.sid)['sources']) == 2
        response = client.get('/api/episodes/sources', query_string={'session_id': w.sid})
        assert response.status_code == 200
        assert response.headers['Cache-Control'] == 'no-store'
        assert response.headers['X-Content-Type-Options'] == 'nosniff'
        assert response.json['scope']['dataset_epoch'] == w.epoch
        assert [item['message_id'] for item in response.json['sources']] == [w.assistant, w.user]
    for query in ('', '?session_id=true', '?session_id=0', '?session_id=1&session_id=2', '?session_id=1&extra=no'):
        assert client.get('/api/episodes/sources' + query).status_code == 400
    assert client.get('/api/episodes/sources?session_id=999999').status_code == 404
    assert not [statement for statement in statements if statement.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE', 'CREATE', 'ALTER'))]
    assert w.ds.get_message_count(w.sid) == 2
