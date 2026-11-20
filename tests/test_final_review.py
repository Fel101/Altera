"""Regression checks for privacy cleanup and lossless, atomic conversation imports."""
import io
import sqlite3
import threading
from types import SimpleNamespace as NS

import pytest

from core.app import create_app
from core.datastore import get_datastore
from core.diagnostics import record_usage, summarize
from core.state_gate import STATE
from core.turns import PROCESS_ID, get_turn_service


@pytest.fixture
def review_app(altera_config):
    altera_config.write_text('voice:\n  tts_enabled: false\nimagegen:\n  enabled: false\n', encoding='utf-8')
    return create_app(autostart=False)


@pytest.fixture
def clear_world(review_app, monkeypatch):
    from core import companion, memory, notes
    calls = []
    store = get_datastore()
    sid = store.create_session('Retained until cleanup completes')
    store.save_message(sid, 'user', 'Private conversation')
    avatars = NS(list=lambda: [{'id': 'fixture-companion'}],
                 update_state=lambda *a, **k: calls.append('state'))
    services = NS(avatars=avatars, photos=NS(delete_all=lambda: calls.append('photos')))
    manager = NS(clear_all_memories=lambda: calls.append('memories'))
    notebook = NS(clear_all=lambda: calls.append('notes'))
    monkeypatch.setattr(companion, 'get_services', lambda: services)
    monkeypatch.setattr(memory, 'get_memory_manager', lambda: manager)
    monkeypatch.setattr(notes, 'NotesStore', lambda avatars: notebook)
    return NS(store=store, sid=sid, calls=calls, manager=manager, notebook=notebook, client=review_app.test_client())


@pytest.mark.parametrize('job', ['altera-photo', 'altera-notes', 'request:system.system_health'])
def test_clear_refuses_active_work_before_deleting_anything(clear_world, job):
    lease = STATE.enter(job)
    try:
        response = clear_world.client.post('/api/clear-data', json={'confirm': 'delete-all'})
        assert response.status_code == 409
        assert clear_world.calls == [] and clear_world.store.get_message_count(clear_world.sid) == 1
        assert not STATE.maintenance
    finally:
        lease.release()


@pytest.mark.parametrize('failed_store', ['manager', 'notebook'])
def test_clear_reports_incomplete_cleanup_and_preserves_conversations(clear_world, monkeypatch, failed_store):
    def broken(*args):
        raise OSError('simulated storage failure')
    target = getattr(clear_world, failed_store)
    monkeypatch.setattr(target, 'clear_all_memories' if failed_store == 'manager' else 'clear_all', broken)
    response = clear_world.client.post('/api/clear-data', json={'confirm': 'delete-all'})
    assert response.status_code == 500 and response.json['code'] == 'clear_incomplete'
    assert 'Some data may remain' in response.json['error']
    assert clear_world.store.get_message_count(clear_world.sid) == 1 and not STATE.maintenance


def test_clear_resets_all_auxiliary_stores_and_blocks_stale_tabs(clear_world):
    old_epoch = clear_world.store.dataset_epoch()['value']
    response = clear_world.client.post('/api/clear-data', json={'confirm': 'delete-all'})
    assert response.status_code == 200
    assert clear_world.calls == ['memories', 'photos', 'notes', 'state']
    assert clear_world.store.get_session(clear_world.sid) is None
    epoch = clear_world.store.dataset_epoch()
    assert epoch['value'] != old_epoch and epoch['required']
    stale = clear_world.client.post('/api/sessions', json={'name': 'Stale tab'}, headers={'X-Altera-Dataset': old_epoch})
    assert stale.status_code == 409 and stale.json['code'] == 'dataset_changed'


def test_fresh_memory_store_can_clear_an_empty_collection_without_a_model():
    from core.memory import MemoryStore
    calls = []
    store = MemoryStore('unused-fixture-path')
    store._client = NS(list_collections=list, get_or_create_collection=lambda **kwargs: calls.append('open') or object(),
                       delete_collection=lambda name: calls.append('delete'))
    store.clear_all()
    assert calls == ['open', 'delete'] and store._collection is None


def test_database_reset_and_stale_tab_barrier_are_atomic():
    store = get_datastore()
    sid = store.create_session('Must survive a failed reset')
    store.save_message(sid, 'user', 'Keep until reset commits')
    before = store.dataset_epoch()
    with store._connect() as conn:
        conn.execute("CREATE TRIGGER reject_new_epoch BEFORE INSERT ON user_profile WHEN NEW.key='dataset_epoch' BEGIN SELECT RAISE(ABORT,'fixture'); END")
    with pytest.raises(sqlite3.IntegrityError):
        store.reset_all_user_data()
    assert store.get_message_count(sid) == 1 and store.get_key('dataset_epoch') == before


def test_notebook_clear_includes_corrupt_or_missing_avatar_records(tmp_path):
    from core.notes import NotesStore
    from core.avatars import AvatarStore
    avatars = AvatarStore(tmp_path / 'avatars')
    orphan = avatars.root / 'orphaned-companion' / 'notes'
    (orphan / 'history').mkdir(parents=True)
    (orphan / 'about_you.md').write_text('Private original', 'utf-8')
    (orphan / 'history/previous.md').write_text('Private previous', 'utf-8')
    assert avatars.list() == []
    NotesStore(avatars).clear_all()
    assert not orphan.exists() and orphan.parent.exists()


def test_privacy_clear_erases_current_and_legacy_speech_caches(altera_config, monkeypatch):
    from core import tts
    from core.paths import root
    directories = [root() / 'data/cache/tts', root() / 'static/audio/tts']
    monkeypatch.setattr(tts, 'AUDIO_CACHE_DIR', str(directories[0]))
    for directory in directories:
        directory.mkdir(parents=True)
        (directory / 'private.wav').write_bytes(b'private speech')
        (directory / 'unfinished.wav.tmp').write_bytes(b'partial private speech')
    tts.clear_cache()
    assert all(not list(directory.iterdir()) for directory in directories)


def test_privacy_clear_reports_an_unremovable_speech_cache(clear_world, monkeypatch):
    from core import tts
    def broken():
        raise PermissionError('Synthetic speech cache write lock')
    monkeypatch.setattr(tts, 'clear_cache', broken)
    response = clear_world.client.post('/api/clear-data', json={'confirm': 'delete-all'})
    assert response.status_code == 500 and response.json['code'] == 'clear_incomplete'
    assert clear_world.store.get_message_count(clear_world.sid) == 1


def test_closed_reply_speech_worker_keeps_privacy_cleanup_blocked(clear_world):
    from core.voice_layer import SpeechStream
    started, release, done = threading.Event(), threading.Event(), threading.Event()
    def synth(text):
        started.set()
        release.wait(5)
        return '/synthetic.wav'
    stream = SpeechStream(synth=synth)
    try:
        stream._submit('Synthetic short speech.')
        assert started.wait(3)
        stream._futures[0].add_done_callback(lambda future: done.set())
        stream.close()
        response = clear_world.client.post('/api/clear-data', json={'confirm': 'delete-all'})
        assert response.status_code == 409 and clear_world.calls == []
    finally:
        release.set()
        stream.close()
        assert done.wait(3)
    assert clear_world.client.post('/api/clear-data', json={'confirm': 'delete-all'}).status_code == 200


def upload(client, sid, content):
    return client.post(f'/api/sessions/{sid}/import-markdown',
                       data={'file': (io.BytesIO(content), 'conversation.md')})


def test_markdown_round_trip_preserves_rules_dashes_and_fenced_headings(review_app):
    store, client = get_datastore(), review_app.test_client()
    first, second = store.create_session('Original'), store.create_session('Imported')
    bodies = ['Before a---b\n\n---\n\nAfter the rule',
              '```markdown\n### You — 2026-10-26 12:00\n\nQuoted heading\n```\n\nStill the same message']
    for role, content in zip(['user', 'assistant'], bodies):
        store.save_message(first, role, content)
    exported = client.get(f'/api/sessions/{first}/export')
    response = upload(client, second, exported.data.replace(b'\n', b'\r\n'))
    assert response.status_code == 200 and response.json['messages_imported'] == 2
    assert [m.content for m in store.get_messages(second)] == bodies


def test_markdown_import_accepts_exports_made_before_the_altera_rename(review_app):
    store, client = get_datastore(), review_app.test_client()
    sid = store.create_session('Older export')
    legacy = '### You — 2026-10-26 12:00\n\nHello\n\n---\n\n### Her — 2026-10-26 12:01\n\nHi there\n'.encode()
    response = upload(client, sid, legacy)
    assert response.status_code == 200 and response.json['messages_imported'] == 2
    assert [(m.role, m.content) for m in store.get_messages(sid)] == [('user', 'Hello'), ('assistant', 'Hi there')]


def test_markdown_import_requires_an_existing_target(review_app):
    response = upload(review_app.test_client(), 999999, b'### You -- absent\n\nText')
    assert response.status_code == 404 and get_datastore().get_message_count(999999) == 0


@pytest.mark.parametrize('body', [b'\xff\xfe', b'No conversation headings', '### You — 2026-10-26 12:00\n\n---\n'.encode()])
def test_invalid_markdown_is_rejected_without_partial_messages(review_app, body):
    store = get_datastore()
    sid = store.create_session('Import target')
    response = upload(review_app.test_client(), sid, body)
    assert response.status_code == 400 and store.get_message_count(sid) == 0


def test_markdown_import_refuses_an_active_reply(review_app):
    store = get_datastore()
    sid = store.create_session('Busy import target')
    turns = get_turn_service(store)
    with turns.connection() as conn:
        conn.execute("INSERT INTO chat_turns(request_id,session_id,fingerprint,status,process_id) VALUES(?,?,?,'streaming',?)",
                     ('busy-import-review', sid, 'test-only', PROCESS_ID))
    response = upload(review_app.test_client(), sid, '### You — 2026-10-26 12:00\n\nNew text\n\n---\n'.encode())
    assert response.status_code == 409 and store.get_message_count(sid) == 0


def test_import_rolls_back_every_message_if_a_later_insert_fails():
    store = get_datastore()
    sid = store.create_session('Atomic import')
    with store._connect() as conn:
        conn.execute("CREATE TRIGGER reject_import BEFORE INSERT ON messages WHEN NEW.content='reject' BEGIN SELECT RAISE(ABORT,'fixture'); END")
    with pytest.raises(sqlite3.IntegrityError):
        store.append_imported_messages(sid, [('user', 'first insert'), ('assistant', 'reject')])
    assert store.get_message_count(sid) == 0


def test_single_conversation_export_has_no_silent_ten_thousand_message_cap(review_app):
    store = get_datastore()
    sid = store.create_session('Complete export')
    store.append_imported_messages(sid, [('user', f'Export row {i}') for i in range(10001)])
    exported = review_app.test_client().get(f'/api/sessions/{sid}/export')
    assert exported.status_code == 200 and exported.data.count(b'### You') == 10001
    assert b'Export row 0\n' in exported.data and b'Export row 10000\n' in exported.data


def test_diagnostics_ignores_oversized_restored_numbers_without_overflow():
    huge = 10 ** 1000
    metrics = summarize({'calls': [{'prompt_tokens': huge, 'reported_cost_credits': huge}],
                         'visible_output_tokens_estimate': huge})
    assert metrics['prompt_tokens']['value'] is None and metrics['reported_cost_credits']['value'] is None
    record = {'cost_unit': 'OpenRouter credits'}
    record_usage(record, {'cost': huge, 'prompt_tokens': huge})
    assert record == {'cost_unit': 'OpenRouter credits'}
