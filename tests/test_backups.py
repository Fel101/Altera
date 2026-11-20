"""Real SQLite/filesystem backup round trips with isolated vector fixtures."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import stat
import zipfile

import pytest
import yaml

from core.backups import BackupError, BackupService, validate_browser_state
from core.datastore import DataStore
from core.state_gate import StateBusy, StateGate


class Vectors:
    def __init__(self):
        self.data = {'version': 1, 'embedding_function': 'test-vectors', 'records': [
            {'id': 'm1', 'content': 'Likes tea', 'metadata': {'avatar_id': 'mara-bell-47ab97', 'confidence': .8}, 'embedding': [.1, .2, .3]}]}
        self.fail = 0

    def snapshot(self):
        return deepcopy(self.data)

    def restore_snapshot(self, data):
        self.data = deepcopy(data)
        if self.fail:
            self.data['records'] = []
            self.fail -= 1
            raise OSError('simulated vector write failure')


def test_room_preference_is_validated_for_portable_backup():
    for mode in ('off', 'day', 'night', 'auto'):
        validate_browser_state({'preferences': {'altera_room': mode}, 'drafts': []})
    with pytest.raises(BackupError):
        validate_browser_state({'preferences': {'altera_room': 'untrusted'}, 'drafts': []})


def test_browser_settings_from_a_backup_made_under_the_old_name_restore_under_the_new_one():
    old = {'her_theme': 'light', 'her_accent': '#112233', 'her_room': 'night', 'her_dismissed_hints': '[]'}
    assert validate_browser_state({'preferences': old, 'drafts': []})['preferences'] == {
        'altera_theme': 'light', 'altera_accent': '#112233', 'altera_room': 'night', 'altera_dismissed_hints': '[]'}
    with pytest.raises(BackupError):
        validate_browser_state({'preferences': {'her_theme': 'light', 'altera_theme': 'dark'}, 'drafts': []})
    with pytest.raises(BackupError):
        validate_browser_state({'preferences': {'her_room': 'untrusted'}, 'drafts': []})


def write_yaml(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data), encoding='utf-8')


@pytest.fixture
def backup_world(tmp_path):
    root = tmp_path / 'app'
    root.mkdir()
    write_yaml(root / 'config.yaml', {'app': {'host': '127.0.0.1', 'port': 5001}, 'tools': {'enabled': False},
                                    'llm': {'provider': 'openrouter', 'model': 'archived-cloud-model'},
                                    'voice': {'tts_engine': 'edge', 'cloud_fallback': True}, 'memory': {'top_k': 5}})
    write_yaml(root / 'config.local.yaml', {'llm': {'providers': {'openrouter': {'api_key': 'fixture-source-key'}}},
                                          'remote_access': {'enabled': True, 'allowed_logins': ['fixture-source-owner']}})
    write_yaml(root / 'personality.yaml', {'name': 'Altera', 'traits': ['curious']})
    write_yaml(root / 'user_profile.yaml', {'name': 'Original profile', 'preferences': {}})
    avatar = root / 'data/avatars/mara-bell-47ab97'
    avatar.mkdir(parents=True)
    (avatar / 'avatar.json').write_text(json.dumps({'id': avatar.name, 'name': 'Mara', 'card': {'name': 'Mara', 'age': 28}}), 'utf-8')
    (avatar / 'portrait.png').write_bytes(b'fixture portrait')
    (avatar / 'notes').mkdir()
    (avatar / 'notes/about_you.md').write_text('- Likes tea', 'utf-8')
    photos = root / 'data/photos'
    photos.mkdir()
    (photos / 'p-0123456789.png').write_bytes(b'fixture picture')
    (root / 'data/secret_key').write_text('destination-session-key', 'utf-8')
    (root / 'workspace').mkdir()
    (root / 'workspace/note.txt').write_text('Original workspace note', 'utf-8')
    store = DataStore(str(root / 'data/conversations.db'))
    sid = store.create_session('Original chat')
    store.save_message(sid, 'user', 'Hello')
    store.save_message(sid, 'assistant', 'Hi')
    store.set_key('adult_mode', {'enabled': True, 'confirmed_at': 'fixture'})
    memory = Vectors()
    service = BackupService(root, store, memory, gate=StateGate())
    return type('World', (), {'root': root, 'store': store, 'sid': sid, 'memory': memory, 'service': service})()


def newer(world):
    world.store.save_message(world.sid, 'user', 'Newer message')
    world.memory.data['records'].append({'id': 'm2', 'content': 'Newer memory', 'metadata': {'type': 'fact'}, 'embedding': [.3, .2, .1]})
    write_yaml(world.root / 'user_profile.yaml', {'name': 'Current profile'})
    write_yaml(world.root / 'config.yaml', {'app': {'host': '127.0.0.1', 'port': 9999}, 'tools': {'enabled': False},
                                          'llm': {'provider': 'lmstudio', 'model': 'current-local'},
                                          'voice': {'tts_engine': 'kokoro', 'cloud_fallback': False}, 'memory': {'top_k': 9}})
    write_yaml(world.root / 'config.local.yaml', {'llm': {'providers': {'openrouter': {'api_key': 'fixture-current-key'}}},
                                                'remote_access': {'enabled': True, 'allowed_logins': ['fixture-current-owner']}})
    (world.root / 'workspace/note.txt').write_text('Current workspace note', 'utf-8')
    (world.root / 'data/photos/newer.png').write_bytes(b'newer picture')


def contents(world):
    return {'messages': [m.content for m in world.store.get_messages(world.sid)], 'memories': world.memory.snapshot(),
            'profile': (world.root / 'user_profile.yaml').read_bytes(), 'settings': (world.root / 'config.yaml').read_bytes(),
            'private': (world.root / 'config.local.yaml').read_bytes(), 'workspace': (world.root / 'workspace/note.txt').read_bytes()}


def altered(source, target, change):
    with zipfile.ZipFile(source) as archive:
        entries = {item.filename: archive.read(item) for item in archive.infolist()}
    change(entries)
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return target


def rehash(entries, name):
    manifest = json.loads(entries['manifest.json'])
    manifest['files'][name] = {'bytes': len(entries[name]), 'sha256': hashlib.sha256(entries[name]).hexdigest()}
    entries['manifest.json'] = json.dumps(manifest).encode()


def test_backup_round_trip_and_pre_restore_recovery(backup_world):
    w = backup_world
    saved = w.service.create()
    assert saved['counts']['messages'] == 2 and saved['counts']['memories'] == 1
    with zipfile.ZipFile(w.service.archive_path(saved['id'])) as archive:
        config = archive.read('preferences/config.yaml')
        assert b'fixture-source-key' not in config and b'fixture-source-owner' not in config
        assert not any('secret_key' in n or 'config.local.yaml' in n for n in archive.namelist())
    newer(w)
    before_epoch = w.store.dataset_epoch()['value']
    preview = w.service.preview(w.service.archive_path(saved['id']))
    assert contents(w)['messages'][-1] == 'Newer message'  # preview is read-only
    result = w.service.apply(preview['token'])
    assert [m.content for m in w.store.get_messages(w.sid)] == ['Hello', 'Hi']
    assert len(w.memory.data['records']) == 1
    assert yaml.safe_load((w.root / 'user_profile.yaml').read_text())['name'] == 'Original profile'
    assert (w.root / 'workspace/note.txt').read_text() == 'Original workspace note'
    assert not (w.root / 'data/photos/newer.png').exists()
    assert (w.root / 'data/secret_key').read_text() == 'destination-session-key'
    config = w.service._settings()
    assert config['llm']['model'] == 'current-local' and config['voice']['cloud_fallback'] is False
    assert config['app']['port'] == 9999 and config['memory']['top_k'] == 5
    assert config['llm']['providers']['openrouter']['api_key'] == 'fixture-current-key'
    assert config['remote_access']['allowed_logins'] == ['fixture-current-owner']
    assert w.store.get_key('adult_mode')['enabled'] is False
    assert result['dataset_epoch'] != before_epoch and w.store.dataset_epoch()['required'] is True
    recovery = w.service.preview(w.service.archive_path(result['recovery_backup']['id']))
    assert recovery['counts']['messages'] == 3 and recovery['counts']['memories'] == 2
    assert not w.service.journal.exists()
    count = len(w.service.list())
    assert w.service.apply(preview['token']) == result
    assert len(w.service.list()) == count


def test_connection_preferences_require_explicit_preview_opt_in(backup_world):
    w = backup_world
    saved = w.service.create()
    newer(w)
    preview = w.service.preview(w.service.archive_path(saved['id']), restore_connections=True)
    assert preview['connections']['cloud_voice_fallback'] is True
    w.service.apply(preview['token'])
    assert w.service._settings()['llm']['model'] == 'archived-cloud-model'
    assert w.service._settings()['voice']['cloud_fallback'] is True
    assert w.service._settings()['llm']['providers']['openrouter']['api_key'] == 'fixture-current-key'


def test_database_failure_rolls_back_vectors_files_and_preferences(backup_world, monkeypatch):
    w = backup_world
    saved = w.service.create()
    newer(w)
    expected = contents(w)
    preview = w.service.preview(w.service.archive_path(saved['id']))
    original = w.service._sqlite_copy
    def fail_after_copy(source, destination):
        original(source, destination)
        if Path(source).name == 'ready.sqlite3':
            raise OSError('simulated disk failure after replacement')
    monkeypatch.setattr(w.service, '_sqlite_copy', fail_after_copy)
    with pytest.raises(BackupError, match='previous data was recovered'):
        w.service.apply(preview['token'])
    assert contents(w) == expected
    assert not w.service.gate.fault and not w.service.journal.exists()
    assert len(w.service.list()) == 2


def test_failed_rollback_stays_closed_and_new_service_recovers_using_the_saved_archive(backup_world):
    w = backup_world
    saved = w.service.create()
    newer(w)
    expected = contents(w)
    preview = w.service.preview(w.service.archive_path(saved['id']))
    w.memory.fail = 2
    with pytest.raises(BackupError, match='automatic rollback failed'):
        w.service.apply(preview['token'])
    assert w.service.gate.fault and w.service.journal.exists()
    with pytest.raises(StateBusy):
        w.service.gate.enter('chat')
    journal = w.service._recovery_record()
    restarted = BackupService(w.root, w.store, w.memory, gate=StateGate())
    assert restarted.gate.fault
    recovery = restarted.preview(restarted.archive_path(journal['recovery_backup']))
    restarted.apply(recovery['token'])
    assert contents(w) == expected
    assert not restarted.gate.fault and not restarted.journal.exists()


def test_restore_retires_all_current_request_ids_without_migrating_live_database(backup_world, monkeypatch):
    w = backup_world
    saved = w.service.create()
    with sqlite3.connect(w.store.db_path) as conn:
        conn.executemany('INSERT INTO chat_request_tombstones(request_id) VALUES(?)',
                         [(f'retired-request-{i:04}',) for i in range(505)])
        conn.execute("INSERT INTO chat_turns(request_id,session_id,fingerprint,process_id,status) VALUES(?,?,?,?,?)",
                     ('recent-request-id', w.sid, 'fingerprint', 'process', 'complete'))
    preview = w.service.preview(w.service.archive_path(saved['id']))
    # The request-history preflight must not invoke the schema-migrating helper.
    prepare = w.service._prepare_restored_requests
    def checked_prepare(*args):
        with monkeypatch.context() as isolated:
            isolated.setattr(w.store, '_connect', lambda: (_ for _ in ()).throw(AssertionError('live database was migrated')))
            return prepare(*args)
    monkeypatch.setattr(w.service, '_prepare_restored_requests', checked_prepare)
    result = w.service.apply(preview['token'])
    with sqlite3.connect(w.store.db_path) as conn:
        retired = {row[0] for row in conn.execute('SELECT request_id FROM chat_request_tombstones')}
        epoch = json.loads(conn.execute("SELECT value FROM user_profile WHERE key='dataset_epoch'").fetchone()[0])
    assert retired == {f'retired-request-{i:04}' for i in range(505)} | {'recent-request-id'}
    assert epoch == {'value': result['dataset_epoch'], 'required': True}


@pytest.mark.parametrize('damage', ['corrupt', 'missing', 'missing-table'])
def test_damaged_current_history_preserves_original_recovery_journal(backup_world, damage):
    w = backup_world
    saved = w.service.create()
    newer(w)
    preview = w.service.preview(w.service.archive_path(saved['id']))
    w.memory.fail = 2
    with pytest.raises(BackupError, match='automatic rollback failed'):
        w.service.apply(preview['token'])
    journal = w.service._recovery_record()
    original_journal = w.service.journal.read_bytes()
    original_archive = w.service.archive_path(journal['recovery_backup']).read_bytes()
    recovery = w.service.preview(w.service.archive_path(journal['recovery_backup']))
    work_before = {entry.name for entry in w.service.work.iterdir()}
    database = Path(w.store.db_path)
    if damage == 'corrupt':
        database.write_bytes(b'synthetic damaged database')
    elif damage == 'missing':
        database.unlink()
    else:
        with sqlite3.connect(database) as conn:
            conn.execute('DROP TABLE chat_request_tombstones')
    before = database.read_bytes() if database.exists() else None
    with pytest.raises(BackupError, match='Current reply history could not be verified'):
        w.service.apply(recovery['token'])
    assert (database.read_bytes() if database.exists() else None) == before
    assert w.service.journal.read_bytes() == original_journal
    assert w.service.archive_path(journal['recovery_backup']).read_bytes() == original_archive
    assert w.service.gate.fault
    assert {entry.name for entry in w.service.work.iterdir()} == work_before


@pytest.mark.parametrize('name', ['files/data/../escape', 'files/data/photos/a:secret', '/absolute', 'files/workspace/CON.txt'])
def test_unsafe_archive_paths_are_rejected_before_any_replacement(backup_world, tmp_path, name):
    w = backup_world
    saved = w.service.create()
    before = contents(w)
    def change(entries):
        entries[name] = b'unsafe'
        rehash(entries, name)
    bad = altered(w.service.archive_path(saved['id']), tmp_path / 'bad.zip', change)
    with pytest.raises(BackupError):
        w.service.preview(bad)
    assert contents(w) == before


def test_recomputed_manifest_cannot_import_credentials_or_protected_runtime_files(backup_world, tmp_path):
    w = backup_world
    saved = w.service.create()
    def inject(entries):
        entries['preferences/config.yaml'] = yaml.safe_dump({'llm': {'api_key': 'not-importable'}}).encode()
        rehash(entries, 'preferences/config.yaml')
    bad = altered(w.service.archive_path(saved['id']), tmp_path / 'bad.zip', inject)
    with pytest.raises(BackupError, match='credentials'):
        w.service.preview(bad)
    def protected(entries):
        entries['files/data/secret_key'] = b'not-importable'
        rehash(entries, 'files/data/secret_key')
    bad = altered(w.service.archive_path(saved['id']), tmp_path / 'protected.zip', protected)
    with pytest.raises(BackupError, match='protected runtime'):
        w.service.preview(bad)


@pytest.mark.parametrize('name', ['SECRET_KEY', 'Altera.Log', 'Voice_Service.Log', 'CACHE', 'CHROMA_DB',
                                  'POLISH-QA', 'Polish-Baseline-review', 'snapshot.BEFORE-repair', 'scratch.TMP'])
def test_archive_cannot_replace_protected_runtime_files_using_different_case(backup_world, tmp_path, name):
    w = backup_world
    saved = w.service.create()
    original_key = (w.root / 'data/secret_key').read_bytes()
    def inject(entries):
        member = f'files/data/{name}'
        entries[member] = b'archive-controlled-runtime-data'
        rehash(entries, member)
    archive = altered(w.service.archive_path(saved['id']), tmp_path / 'case-variant.zip', inject)
    with pytest.raises(BackupError, match='protected runtime'):
        w.service.preview(archive)
    assert (w.root / 'data/secret_key').read_bytes() == original_key
    assert not list(w.service.staging.iterdir())


def test_integrity_and_symlink_entries_are_rejected(backup_world, tmp_path):
    w = backup_world
    saved = w.service.create()
    bad = altered(w.service.archive_path(saved['id']), tmp_path / 'hash.zip', lambda entries: entries.__setitem__('memories.json', b'{}'))
    with pytest.raises(BackupError):
        w.service.preview(bad)
    with zipfile.ZipFile(w.service.archive_path(saved['id'])) as source, zipfile.ZipFile(tmp_path / 'link.zip', 'w') as target:
        for info in source.infolist():
            if info.filename == 'memories.json':
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
            target.writestr(info, source.read(info))
    with pytest.raises(BackupError):
        w.service.preview(tmp_path / 'link.zip')


def test_archive_sql_triggers_are_not_imported_or_executed(backup_world, tmp_path):
    w = backup_world
    saved = w.service.create()
    def inject(entries):
        path = tmp_path / 'database.sqlite3'
        path.write_bytes(entries['database.sqlite3'])
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TRIGGER injected AFTER INSERT ON messages BEGIN DELETE FROM user_profile; END")
        entries['database.sqlite3'] = path.read_bytes()
        rehash(entries, 'database.sqlite3')
    archive = altered(w.service.archive_path(saved['id']), tmp_path / 'trigger.zip', inject)
    preview = w.service.preview(archive)
    w.service.apply(preview['token'])
    with sqlite3.connect(w.store.db_path) as conn:
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='injected'").fetchone()
    w.store.save_message(w.sid, 'user', 'Safe insert')
    assert w.store.get_key('dataset_epoch')


def test_busy_background_work_prevents_snapshots(backup_world):
    w = backup_world
    lease = w.service.gate.enter('picture generation')
    try:
        with pytest.raises(StateBusy):
            w.service.create()
    finally:
        lease.release()
    with w.service.gate.exclusive():
        with pytest.raises(StateBusy):
            w.service.gate.enter('new chat')


def test_backup_preserves_memory_sources_and_recall_boundary(backup_world):
    from core.knowledge import reference, can_recall
    w = backup_world
    sid = w.store.ensure_avatar_session('mara-bell-47ab97', 'Mara')
    mid = w.store.save_message(sid, 'user', 'Likes tea')
    w.memory.data['records'][0]['metadata']['source_refs'] = json.dumps([reference(w.store.get_message(mid))])
    w.store.set_key('knowledge_recall:mara-bell-47ab97', {'through': mid - 1})
    saved = w.service.create()
    w.store.change_message(mid, sid, content='Changed after backup')
    preview = w.service.preview(w.service.archive_path(saved['id']))
    w.service.apply(preview['token'])
    assert w.store.get_key('knowledge_recall:mara-bell-47ab97')['through'] == mid - 1
    assert can_recall(w.memory.data['records'][0], w.store)


def test_browser_drafts_round_trip_and_recovery_keeps_current_drafts(backup_world):
    w = backup_world
    epoch = w.store.dataset_epoch()['value']
    original = {'dataset_epoch': epoch, 'preferences': {'altera_theme': 'light', 'altera_accent': '#123456', 'altera_room': 'auto'},
                'drafts': [{'session_id': w.sid, 'message': 'Original unsent draft'}]}
    saved = w.service.create(original)
    current = {**original, 'drafts': [{'session_id': w.sid, 'message': 'Current unsent draft'}]}
    preview = w.service.preview(w.service.archive_path(saved['id']))
    assert preview['counts']['unsent_drafts'] == 1
    result = w.service.apply(preview['token'], current_browser_state=current)
    assert result['browser_state']['drafts'] == original['drafts']
    assert result['browser_state']['preferences']['altera_room'] == 'auto'
    with zipfile.ZipFile(w.service.archive_path(result['recovery_backup']['id'])) as archive:
        browser = json.loads(archive.read('preferences/browser.json'))
        assert browser['drafts'] == current['drafts']
    with pytest.raises(BackupError, match='older data'):
        w.service.create(original)
    from core.constants import FTS_TABLE
    assert w.store.fts_search('Hello', fts_table_name=FTS_TABLE)[0]['content'] == 'Hello'


def test_invalid_sqlite_upload_is_a_validation_error(backup_world, tmp_path):
    w = backup_world
    saved = w.service.create()
    def inject(entries):
        entries['database.sqlite3'] = b'not a database'
        rehash(entries, 'database.sqlite3')
    archive = altered(w.service.archive_path(saved['id']), tmp_path / 'invalid-db.zip', inject)
    with pytest.raises(BackupError, match='validated'):
        w.service.preview(archive)
    assert not list(w.service.staging.iterdir())


def test_native_chroma_round_trip_uses_saved_embeddings_without_a_model(tmp_path):
    import chromadb
    from chromadb.api.types import EmbeddingFunction
    from core.memory import MemoryStore

    class NoModel(EmbeddingFunction):
        def __init__(self):
            pass
        @staticmethod
        def name():
            return 'altera-test-no-model'
        def get_config(self):
            return {}
        @staticmethod
        def build_from_config(config):
            return NoModel()
        def __call__(self, input):
            raise AssertionError('Restoring saved embeddings must not run a model')

    memory = MemoryStore(str(tmp_path / 'vectors'))
    memory._client = chromadb.PersistentClient(path=memory.persist_directory)
    memory._collection = memory.client.create_collection('altera_memories', embedding_function=NoModel(),
                                                        metadata={'hnsw:space': 'cosine'})
    memory.collection.add(ids=['one', 'two'], documents=['Tea', 'Coffee'], embeddings=[[1., 0., 0.], [0., 1., 0.]])
    saved = memory.snapshot()
    memory.collection.delete(ids=['one'])
    memory.restore_snapshot(saved)
    assert memory.collection.query(query_embeddings=[[1., 0., 0.]], n_results=1)['ids'] == [['one']]
    assert memory.snapshot() == saved


@pytest.fixture
def backup_api(backup_world, monkeypatch):
    from flask import Flask, jsonify
    from core import config, datastore, state_gate
    from core.routes import backups, pages
    root = Path(__file__).resolve().parent.parent
    app = Flask(__name__, template_folder=str(root / 'templates'), static_folder=str(root / 'static'))
    app.secret_key = 'isolated-test'
    app.config['MAX_CONTENT_LENGTH'] = 512
    monkeypatch.setattr(config, 'ROOT', backup_world.root)
    monkeypatch.setattr(datastore, '_datastore_instance', backup_world.store)
    monkeypatch.setattr(state_gate, 'STATE', backup_world.service.gate)
    monkeypatch.setattr(backups, 'get_backup_service', lambda: backup_world.service)
    app.register_blueprint(backups.bp)
    app.context_processor(pages.dataset_context)
    @app.post('/api/change')
    def change():
        return jsonify(changed=True)
    @app.get('/')
    def home():
        return 'home'
    return app.test_client()


def test_backup_api_upload_preview_confirmation_and_dataset_epoch(backup_api, backup_world):
    from io import BytesIO
    client, w = backup_api, backup_world
    saved = client.post('/api/backups', json={'format': 'zip'})
    assert saved.status_code == 201
    downloaded = client.get(saved.json['download_url'])
    assert downloaded.status_code == 200 and 'attachment' in downloaded.headers['Content-Disposition']
    assert len(downloaded.data) > 512  # backup upload has a separate limit from ordinary writes
    preview = client.post('/api/backups/restore/preview', data={'file': (BytesIO(downloaded.data), 'backup.zip')})
    assert preview.status_code == 200
    token = preview.json['token']
    assert client.post('/api/backups/restore/apply', json={'token': token}).status_code == 400
    assert client.post('/api/backups/restore/apply', json={'token': token, 'confirm': 'restore', 'recovery_format': 'zip'}).status_code == 200
    assert client.post('/api/change', json={}).json['code'] == 'dataset_changed'
    epoch = w.store.dataset_epoch()['value']
    assert client.post('/api/change', headers={'X-Altera-Dataset': epoch}).status_code == 200
    bad = client.post('/api/backups/restore/preview', data={'file': (BytesIO(b'bad'), 'backup.zip')})
    assert bad.status_code == 400




def test_backup_listing_and_creation_do_not_initialize_service_during_maintenance(backup_api, backup_world, monkeypatch):
    from core.routes import backups
    monkeypatch.setattr(backups, 'get_backup_service', lambda: pytest.fail('Maintenance must refuse before service initialization.'))
    with backup_world.service.gate.exclusive():
        assert backup_api.get('/api/backups').status_code == 503
        assert backup_api.post('/api/backups', json={'format': 'zip'}).status_code == 503
    assert not backup_world.service.gate.active


def test_real_backup_download_retains_request_lease_until_closed(backup_api, backup_world):
    saved = backup_api.post('/api/backups', json={'format': 'zip'})
    assert saved.status_code == 201
    response = backup_api.get(saved.json['download_url'], buffered=False)
    try:
        assert response.status_code == 200 and backup_world.service.gate.active
        with pytest.raises(StateBusy):
            with backup_world.service.gate.exclusive():
                pytest.fail('An open backup download must retain ownership.')
    finally:
        response.close()
    assert not backup_world.service.gate.active


def test_faulted_backup_list_remains_accessible_but_shutdown_blocks_it(backup_api, backup_world):
    gate = backup_world.service.gate
    gate.fail_closed('Synthetic recovery required')
    listing = backup_api.get('/api/backups')
    assert listing.status_code == 200 and listing.json['recovery_required'] is True
    assert backup_api.post('/api/backups', json={'format': 'zip'}).status_code == 400
    assert not gate.active
    gate.begin_shutdown()
    assert backup_api.get('/api/backups').status_code == 503


def test_failed_restore_keeps_recovery_screen_and_assets_accessible(backup_api, backup_world, monkeypatch):
    w = backup_world
    w.service.gate.fail_closed('Recovery required')
    # The recovery page must not need a usable conversation database.
    monkeypatch.setattr(w.store, 'dataset_epoch', lambda: (_ for _ in ()).throw(OSError('broken store')))
    assert backup_api.get('/').status_code == 503
    assert backup_api.get('/', headers={'Accept': 'text/html'}).location == '/api/backups/recovery'
    assert backup_api.get('/api/backups/recovery').status_code == 200
    assert backup_api.get('/static/js/backups.js').status_code == 200


def test_background_reservation_and_streamed_request_hold_gate(backup_api, backup_world, monkeypatch):
    import threading
    from flask import Response
    from core import state_gate
    w = backup_world
    entered, release = threading.Event(), threading.Event()
    def work():
        entered.set()
        release.wait(5)
    thread = state_gate.start_job(work)
    try:
        assert entered.wait(5)
        with pytest.raises(StateBusy):
            w.service.create()
    finally:
        release.set()
        thread.join(5)
    assert not w.service.gate.active
    app = backup_api.application
    @app.get('/stream')
    def stream():
        return Response(iter([b'first', b'last']))
    response = backup_api.get('/stream', buffered=False)
    assert w.service.gate.active
    response.close()
    assert not w.service.gate.active
