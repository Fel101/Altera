"""Expression is an inspectable, decaying cue with strict source boundaries."""
from datetime import datetime, timedelta, UTC
import json

import pytest

from core.datastore import DataStore, init_db
from core.expression import get_expression, prompt_section, record_exchange

NOW = datetime(2026, 10, 27, 15, tzinfo=UTC)


@pytest.fixture
def expression_store(tmp_path):
    path = str(tmp_path / 'expression.db')
    init_db(path)
    ds = DataStore(path)
    ds.dataset_epoch()
    return ds


def exchange(ds, owner='a', text="I'm happy to hear that."):
    sid = ds.create_session('Chat')
    conn = ds._connect()
    try:
        with conn:
            conn.execute('UPDATE sessions SET avatar_id=? WHERE id=?', (owner, sid))
    finally:
        conn.close()
    user = ds.save_message(sid, 'user', 'I had a good day.')
    assistant = ds.save_message(sid, 'assistant', text)
    return sid, user, assistant


def test_expression_persists_across_sessions_and_decays_without_writes(expression_store):
    ds = expression_store
    sid, user, assistant = exchange(ds)
    assert record_exchange(ds, sid, user, assistant, NOW)
    assert get_expression(ds, 'a', NOW)['state'] == 'happy'
    assert get_expression(ds, 'a', NOW + timedelta(hours=6))['intensity'] == .3
    assert get_expression(ds, 'a', NOW + timedelta(hours=30))['state'] == 'calm'
    assert get_expression(ds, 'b', NOW)['state'] == 'calm'
    # Reading late does not rewrite time or intensify the state on a second read.
    assert get_expression(ds, 'a', NOW)['intensity'] == .6
    extra = ds.create_session('Another chat')
    conn = ds._connect()
    try:
        with conn:
            conn.execute("UPDATE sessions SET avatar_id='a' WHERE id=?", (extra,))
    finally:
        conn.close()
    assert get_expression(ds, 'a', NOW)['frame'] == 'happy'
    conn = ds._connect()
    try:
        saved = dict(conn.execute('SELECT * FROM companion_expression').fetchone())
    finally:
        conn.close()
    assert 'good day' not in json.dumps(saved) and 'happy to hear' not in json.dumps(saved)


@pytest.mark.parametrize('change', ['edit', 'delete', 'owner', 'floor', 'epoch', 'dirty', 'owner_dirty', 'revision'])
def test_stale_sources_never_drive_expression(expression_store, change):
    ds = expression_store
    sid, user, assistant = exchange(ds)
    assert record_exchange(ds, sid, user, assistant, NOW)
    if change == 'floor':
        ds.set_key('knowledge_recall:a', {'through': assistant})
    elif change == 'epoch':
        ds.set_key('dataset_epoch', {'value': 'replacement'})
    elif change == 'dirty':
        ds.set_key(f'knowledge_dirty:{sid}', {'pending': True})
    elif change == 'owner_dirty':
        ds.set_key('knowledge_dirty_owner:a', {'pending': True})
    elif change == 'revision':
        ds.set_key(f'knowledge_dirty:{sid}', {'revision': 1})
    else:
        conn = ds._connect()
        try:
            with conn:
                if change == 'edit':
                    conn.execute('UPDATE messages SET content=? WHERE id=?', ('Changed', user))
                elif change == 'delete':
                    conn.execute('DELETE FROM messages WHERE id=?', (assistant,))
                else:
                    conn.execute('UPDATE sessions SET avatar_id=? WHERE id=?', ('b', sid))
        finally:
            conn.close()
    assert get_expression(ds, 'a', NOW)['state'] == 'calm'


def test_exchange_retries_old_exchanges_and_mixed_sessions_are_rejected(expression_store):
    ds = expression_store
    first = exchange(ds, text="I'm curious about that.")
    second = exchange(ds, text="I'm excited!")
    assert record_exchange(ds, *second, NOW)
    assert not record_exchange(ds, *second, NOW + timedelta(hours=1))
    assert not record_exchange(ds, *first, NOW)
    assert not record_exchange(ds, second[0], first[1], second[2], NOW)
    assert get_expression(ds, 'a', NOW)['state'] == 'excited'


def test_neutral_exchange_returns_calm_and_user_emotion_is_not_companion_expression(expression_store):
    ds = expression_store
    sid, user, assistant = exchange(ds, text='Here is the information you requested.')
    assert record_exchange(ds, sid, user, assistant, NOW)
    assert get_expression(ds, 'a', NOW)['state'] == 'calm'
    assert prompt_section(get_expression(ds, 'a', NOW)) == ''
    cue = prompt_section({'state': 'warm'})
    assert 'simulated expression' in cue and 'not a fact' in cue and 'absence' in cue


@pytest.mark.parametrize('column,value', [('state', 'resentful'), ('intensity', 999),
                                         ('updated_at', 'broken'), ('updated_at', '2027-01-01T00:00:00+00:00')])
def test_malformed_or_future_expression_fails_calm(expression_store, column, value):
    ds = expression_store
    assert record_exchange(ds, *exchange(ds), NOW)
    conn = ds._connect()
    try:
        with conn:
            conn.execute('UPDATE companion_expression SET ' + column + '=?', (value,))
    finally:
        conn.close()
    assert get_expression(ds, 'a', NOW)['state'] == 'calm'


def pending_exchange(ds):
    from core.turns import PROCESS_ID
    sid = ds.create_session('Recovery')
    with ds._connect() as conn:
        conn.execute("UPDATE sessions SET avatar_id='a' WHERE id=?", (sid,))
    user = ds.save_message(sid, 'user', 'Synthetic private words')
    with ds._connect() as conn:
        conn.execute("INSERT INTO chat_turns(request_id,session_id,fingerprint,process_id,status,user_message_id) VALUES(?,?,?,?,?,?)",
                     ('pending-expression-123', sid, 'synthetic', PROCESS_ID, 'streaming', user))
    assistant = ds.save_turn_message('pending-expression-123', sid, 'assistant', "I'm happy to hear that.")
    with ds._connect() as conn:
        pending = conn.execute("SELECT expression_pending FROM chat_turns WHERE request_id='pending-expression-123'").fetchone()[0]
        assert pending and 'private words' not in pending and 'happy to hear' not in pending
        conn.execute("UPDATE chat_turns SET process_id='retired-process' WHERE request_id='pending-expression-123'")
    return sid, user, assistant, json.loads(pending)


@pytest.mark.parametrize('change', ['none', 'user_edit', 'assistant_edit', 'owner', 'floor', 'epoch',
                                    'dirty', 'revision', 'future', 'malformed', 'foreign_answer'])
def test_restart_recovers_expression_only_for_unchanged_saved_sources(expression_store, change):
    from core.turns import TurnService
    ds = expression_store
    sid, user, assistant, pending = pending_exchange(ds)
    if change == 'floor':
        ds.set_key('knowledge_recall:a', {'through': assistant})
    elif change == 'epoch':
        ds.set_key('dataset_epoch', {'value': 'restored'})
    elif change == 'dirty':
        ds.set_key(f'knowledge_dirty:{sid}', {'pending': True})
    elif change == 'revision':
        ds.set_key(f'knowledge_dirty:{sid}', {'revision': 1})
    else:
        with ds._connect() as conn:
            if change == 'user_edit':
                conn.execute('UPDATE messages SET content=? WHERE id=?', ('Different user text', user))
            elif change == 'assistant_edit':
                conn.execute('UPDATE messages SET content=? WHERE id=?', ('Different answer', assistant))
            elif change == 'owner':
                conn.execute("UPDATE sessions SET avatar_id='b' WHERE id=?", (sid,))
            elif change == 'future':
                pending['saved_at'] = (datetime.now(UTC) + timedelta(days=1)).isoformat()
                conn.execute("UPDATE chat_turns SET expression_pending=? WHERE request_id='pending-expression-123'", (json.dumps(pending),))
            elif change == 'malformed':
                conn.execute("UPDATE chat_turns SET expression_pending='invalid' WHERE request_id='pending-expression-123'")
            elif change == 'foreign_answer':
                other = ds.create_session('Other')
                foreign = ds.save_message(other, 'assistant', 'Other answer')
                conn.execute("UPDATE chat_turns SET assistant_message_id=? WHERE request_id='pending-expression-123'", (foreign,))
    TurnService(ds)
    TurnService(ds)  # Recovery cannot update the cue a second time.
    assert get_expression(ds, 'a')['state'] == ('happy' if change == 'none' else 'calm')
    with ds._connect() as conn:
        turn = conn.execute("SELECT status,expression_pending FROM chat_turns WHERE request_id='pending-expression-123'").fetchone()
        assert turn['expression_pending'] is None
        if change == 'none':
            row = conn.execute("SELECT updated_at FROM companion_expression WHERE owner='a'").fetchone()
            assert row['updated_at'] == pending['saved_at']


def test_failed_expression_recovery_rolls_back_intent_and_can_retry(expression_store):
    from core.turns import TurnService
    ds = expression_store
    pending_exchange(ds)
    with ds._connect() as conn:
        conn.execute("""CREATE TRIGGER fail_expression_cleanup BEFORE UPDATE OF expression_pending ON chat_turns
                        WHEN OLD.expression_pending IS NOT NULL AND NEW.expression_pending IS NULL
                        BEGIN SELECT RAISE(ABORT, 'synthetic cleanup failure'); END""")
    TurnService(ds)  # Failure is retained and logged instead of claiming success.
    with ds._connect() as conn:
        assert conn.execute('SELECT count(*) FROM companion_expression').fetchone()[0] == 0
        assert conn.execute("SELECT expression_pending FROM chat_turns WHERE request_id='pending-expression-123'").fetchone()[0]
        conn.execute('DROP TRIGGER fail_expression_cleanup')
    TurnService(ds)
    assert get_expression(ds, 'a')['state'] == 'happy'


@pytest.mark.parametrize('method', ['session', 'owner'])
def test_context_invalidation_clears_pending_expression(expression_store, method):
    from core.expression import invalidate_owner, invalidate_session
    from core.turns import TurnService
    ds = expression_store
    sid, _, _, _ = pending_exchange(ds)
    with ds._connect() as conn:
        if method == 'session':
            invalidate_session(conn, sid)
        else:
            invalidate_owner(conn, 'a')
        assert conn.execute("SELECT expression_pending FROM chat_turns WHERE request_id='pending-expression-123'").fetchone()[0] is None
    TurnService(ds)
    assert get_expression(ds, 'a')['state'] == 'calm'


def test_pending_expression_is_consumed_once_after_normal_save(expression_store):
    from core.expression import consume_pending
    ds = expression_store
    pending_exchange(ds)
    with ds._connect() as conn:
        conn.execute("UPDATE chat_turns SET status='saved' WHERE request_id='pending-expression-123'")
    assert consume_pending(ds, 'pending-expression-123')
    assert not consume_pending(ds, 'pending-expression-123')
    assert get_expression(ds, 'a')['state'] == 'happy'
