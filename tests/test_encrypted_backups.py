"""Encrypted snapshots/restores use real SQLite/files/crypto and synthetic data."""
# Pytest intentionally resolves imported fixture names as function arguments.
from datetime import datetime, UTC
from io import BytesIO
import json
from types import SimpleNamespace

import pytest
from werkzeug.datastructures import MultiDict
import yaml

from core import backup_crypto
from core.backups import BackupError, BackupService, STAGING_OWNER
from core.state_gate import StateBusy, StateGate
from test_backups import altered, backup_api, backup_world, contents, newer, rehash  # noqa: F401

PASSWORD = 'Encrypted fixture passphrase 🔐'
RECOVERY_PASSWORD = 'Independent recovery passphrase 🌿'


def private_headers(response):
    assert response.headers['Cache-Control'] == 'no-store'
    assert response.headers['X-Content-Type-Options'] == 'nosniff'


def assert_no_credentials(service, *secrets):
    for path in service.directory.rglob('*.json'):
        data = json.dumps(json.loads(path.read_text('utf-8')), ensure_ascii=False)
        for secret in secrets:
            if secret is not None:
                assert secret not in data, f'Credential persisted in {path.name}'


def assert_preview_ciphertext_only(service, preview, original):
    folder = service.staging / preview['token']
    assert {path.name for path in folder.iterdir()} == {'preview.json', 'upload.zip', STAGING_OWNER}
    owner = json.loads((folder / STAGING_OWNER).read_text('utf-8'))
    assert set(owner) == {'format', 'version', 'token', 'created_at'} and owner['token'] == preview['token']
    assert (folder / 'upload.zip').read_bytes() == original.read_bytes()
    assert (folder / 'upload.zip').read_bytes().startswith(backup_crypto.MAGIC)
    assert not list(service.work.iterdir())
    record = json.loads((folder / 'preview.json').read_text('utf-8'))
    assert 'password' not in record and 'recovery_key' not in record and 'recovery_password' not in record


def test_encrypted_whole_app_roundtrip_preserves_local_preferences_browser_voice_and_story(backup_world):
    from core.episodes import EpisodeService
    from core.voice_notes import VoiceNoteService
    from test_voice_notes import note_id, recording
    w = backup_world
    voice = VoiceNoteService(w.store, w.root / 'data/voice_notes')
    scope, audio = voice.context(w.sid), recording()
    note = voice.upload(note_id(), w.sid, scope['dataset_epoch'], BytesIO(audio), source_revision=scope['source_revision'],
                        recall_after=scope['recall_after'], avatar_id=scope['avatar_id'])
    episodes = EpisodeService(w.store)
    source = w.store.get_messages(w.sid)[0]
    episode = episodes.capture(w.sid, scope['dataset_epoch'], {'request_id': 'encrypted-story-fixture', 'kind': 'actual_exchange',
        'title': 'A saved textual exchange', 'summary': 'Curated synthetic conversation', 'in_story': True,
        'salience': 5, 'sources': [{'message_id': source.id}]}, owner='')['episode']
    with episodes.connection(write=True) as conn:
        conn.execute('UPDATE companion_episodes SET pinned=1 WHERE id=?', (episode['id'],))
    browser = {'dataset_epoch': scope['dataset_epoch'], 'preferences': {'altera_theme': 'light'},
               'drafts': [{'session_id': w.sid, 'message': 'A synthetic unsent draft'}]}
    saved = w.service.create(browser, password=PASSWORD)
    assert saved['encrypted'] and saved['id'].endswith('.alterabak')
    original = w.service.archive_path(saved['id'])
    assert original.read_bytes().startswith(backup_crypto.MAGIC)
    newer(w)
    voice.delete(note['note_id'])
    with episodes.connection(write=True) as conn:
        conn.execute('DELETE FROM companion_episodes')
    preview = w.service.preview(original, password=PASSWORD)
    assert preview['encrypted'] and preview['counts']['unsent_drafts'] == 1
    assert_preview_ciphertext_only(w.service, preview, original)
    restored = w.service.apply(preview['token'], password=PASSWORD, recovery_password=RECOVERY_PASSWORD)
    assert [message.content for message in w.store.get_messages(w.sid)] == ['Hello', 'Hi']
    assert restored['browser_state']['drafts'] == browser['drafts'] and restored['browser_state']['preferences'] == browser['preferences']
    assert len(w.memory.data['records']) == 1
    assert (w.root / 'workspace/note.txt').read_text() == 'Original workspace note'
    assert yaml.safe_load((w.root / 'user_profile.yaml').read_text())['name'] == 'Original profile'
    settings = w.service._settings()
    assert settings['llm']['model'] == 'current-local' and settings['voice']['cloud_fallback'] is False
    assert settings['app']['port'] == 9999 and settings['memory']['top_k'] == 5
    assert settings['llm']['providers']['openrouter']['api_key'] == 'fixture-current-key'
    assert settings['remote_access']['allowed_logins'] == ['fixture-current-owner']
    assert (w.root / 'data/secret_key').read_text() == 'destination-session-key'
    assert voice.get(note['note_id'])['status'] == 'stale'
    assert voice.audio(note['note_id'])[0].read_bytes() == audio
    with episodes.connection() as conn:
        row = conn.execute('SELECT summary,curation,pinned,in_story,status FROM companion_episodes WHERE id=?', (episode['id'],)).fetchone()
    assert (row['summary'], row['curation'], row['pinned'], row['in_story']) == ('Curated synthetic conversation', 'manual_curated', 1, 1)
    assert row['status'] == 'stale'
    recovery = restored['recovery_backup']
    assert recovery['id'].endswith('.alterabak') and w.service.archive_path(recovery['id']).read_bytes().startswith(backup_crypto.MAGIC)
    recovered_preview = w.service.preview(w.service.archive_path(recovery['id']), password=RECOVERY_PASSWORD)
    assert recovered_preview['counts']['messages'] == 3 and recovered_preview['counts']['memories'] == 2
    assert_no_credentials(w.service, PASSWORD, RECOVERY_PASSWORD)


def test_multipart_spooling_stays_in_private_instance_and_closes_on_invalid_upload(backup_api, backup_world, monkeypatch):
    import tempfile
    from core.private_paths import private_temp_dir
    folder = private_temp_dir(backup_world.root)
    factory, locations = tempfile.SpooledTemporaryFile, []
    def tracked(*args, **kwargs):
        locations.append(kwargs.get('dir'))
        return factory(*args, **kwargs)
    monkeypatch.setattr(tempfile, 'SpooledTemporaryFile', tracked)
    response = backup_api.post('/api/backups/restore/preview', data={
        'file': (BytesIO(b'Not an archive' * 50000), 'invalid.zip')})
    assert response.status_code == 400
    response.close()
    assert locations == [folder] and not list(folder.iterdir())
    assert not list(backup_world.service.staging.iterdir())


@pytest.mark.parametrize('password', ['too-short', '🙂' * 257, '\ud800' * 12])
def test_invalid_creation_password_fails_before_any_plaintext_snapshot(backup_world, monkeypatch, password):
    w = backup_world
    before = contents(w)
    def no_snapshot(*_):
        pytest.fail('Invalid password reached snapshot work')
    monkeypatch.setattr(w.service, '_sqlite_copy', no_snapshot)
    monkeypatch.setattr(w.memory, 'snapshot', no_snapshot)
    with pytest.raises(BackupError):
        w.service.create(password=password)
    assert not list(w.service.directory.glob('altera-*')) and not list(w.service.work.iterdir())
    # Snapshot is patched, so compare application data directly rather than contents().
    assert [m.content for m in w.store.get_messages(w.sid)] == before['messages']


@pytest.mark.parametrize('mode', ['password', 'recovery'])
def test_wrong_source_credential_changes_nothing_and_retains_no_plaintext_staging(backup_world, mode):
    w = backup_world
    key = backup_crypto.generate_recovery_key()
    saved = w.service.create(password=PASSWORD, recovery_key=key)
    before = contents(w)
    credentials = {'password': 'A wrong fixture passphrase'} if mode == 'password' else {'recovery_key': backup_crypto.generate_recovery_key()}
    with pytest.raises(BackupError, match='could not be opened'):
        w.service.preview(w.service.archive_path(saved['id']), **credentials)
    assert contents(w) == before
    assert not list(w.service.staging.iterdir()) and not list(w.service.work.iterdir())
    assert not w.service.journal.exists()
    assert_no_credentials(w.service, PASSWORD, key)


def test_recovery_key_preview_persists_only_ciphertext_and_apply_requires_reentered_source_credential(backup_world):
    w = backup_world
    key = backup_crypto.generate_recovery_key()
    saved = w.service.create(password=PASSWORD, recovery_key=key)
    path = w.service.archive_path(saved['id'])
    preview = w.service.preview(path, recovery_key=key)
    assert_preview_ciphertext_only(w.service, preview, path)
    assert_no_credentials(w.service, PASSWORD, key)
    newer(w)
    before = contents(w)
    with pytest.raises(BackupError, match='could not be opened'):
        w.service.apply(preview['token'], recovery_password=RECOVERY_PASSWORD)
    assert contents(w) == before and not w.service.journal.exists()
    assert_preview_ciphertext_only(w.service, preview, path)
    result = w.service.apply(preview['token'], recovery_key=key, recovery_password=RECOVERY_PASSWORD)
    assert result['status'] == 'restored' and result['recovery_backup']['id'].endswith('.alterabak')
    assert_no_credentials(w.service, PASSWORD, key, RECOVERY_PASSWORD)


def test_encrypted_restore_requires_valid_recovery_password_before_restore_preparation(backup_world, monkeypatch):
    w = backup_world
    saved = w.service.create(password=PASSWORD)
    preview = w.service.preview(w.service.archive_path(saved['id']), password=PASSWORD)
    before = contents(w)
    def no_unpack(*_):
        pytest.fail('Missing/invalid recovery password reached plaintext restore preparation')
    monkeypatch.setattr(w.service, '_unpack', no_unpack)
    with pytest.raises(BackupError, match='encrypted recovery backup'):
        w.service.apply(preview['token'], password=PASSWORD)
    with pytest.raises(BackupError):
        w.service.apply(preview['token'], password=PASSWORD, recovery_password='short')
    assert contents(w) == before and not w.service.journal.exists()
    assert not list(w.service.work.iterdir())


def test_encrypted_list_hides_counts_content_and_recovery_secrets_without_unlocking(backup_world, monkeypatch):
    w = backup_world
    key = backup_crypto.generate_recovery_key()
    saved = w.service.create(password=PASSWORD, recovery_key=key)
    def no_decrypt(*_, **__):
        pytest.fail('Listing encrypted backups tried to decrypt')
    monkeypatch.setattr(backup_crypto, 'decrypt_file', no_decrypt)
    listed = w.service.list()
    assert len(listed) == 1 and listed[0]['id'] == saved['id']
    assert listed[0]['counts'] == {} and listed[0]['encrypted'] is True
    serialized = json.dumps(listed, ensure_ascii=False)
    for secret in [PASSWORD, key, 'Hello', 'Original profile', 'fixture-source-key']:
        assert secret not in serialized
    assert 'recovery_key' not in listed[0]
    assert_no_credentials(w.service, PASSWORD, key)


@pytest.mark.parametrize('phase', ['before preview', 'after preview'])
def test_corruption_before_or_after_preview_never_replaces_application_data(backup_world, tmp_path, phase):
    w = backup_world
    saved = w.service.create(password=PASSWORD)
    original = w.service.archive_path(saved['id'])
    before = contents(w)
    if phase == 'before preview':
        corrupted = tmp_path / 'bad.alterabak'
        raw = bytearray(original.read_bytes())
        raw[-1] ^= 1
        corrupted.write_bytes(raw)
        with pytest.raises(BackupError, match='could not be opened'):
            w.service.preview(corrupted, password=PASSWORD)
        assert not list(w.service.staging.iterdir())
    else:
        preview = w.service.preview(original, password=PASSWORD)
        upload = w.service.staging / preview['token'] / 'upload.zip'
        raw = bytearray(upload.read_bytes())
        raw[-1] ^= 1
        upload.write_bytes(raw)
        with pytest.raises(BackupError, match='archive changed'):
            w.service.apply(preview['token'], password=PASSWORD, recovery_password=RECOVERY_PASSWORD)
    assert contents(w) == before and not w.service.journal.exists() and not list(w.service.work.iterdir())


@pytest.mark.parametrize('malice', ['unsafe member', 'credentials'])
def test_authenticated_encryption_does_not_bypass_logical_zip_validation(backup_world, tmp_path, malice):
    w = backup_world
    original = w.service.create()
    def poison(entries):
        if malice == 'unsafe member':
            member = 'files/data/../escape.txt'
            entries[member] = b'unsafe fixture'
        else:
            member = 'preferences/config.yaml'
            entries[member] = yaml.safe_dump({'llm': {'api_key': 'archive-injected-credential'}}).encode()
        rehash(entries, member)
    bad = altered(w.service.archive_path(original['id']), tmp_path / 'bad.zip', poison)
    encrypted = tmp_path / 'authenticated-malicious.alterabak'
    backup_crypto.encrypt_file(bad, encrypted, PASSWORD)
    before = contents(w)
    with pytest.raises(BackupError, match='unsafe path|credentials'):
        w.service.preview(encrypted, password=PASSWORD)
    assert contents(w) == before and not list(w.service.staging.iterdir()) and not list(w.service.work.iterdir())


def test_encrypted_publication_preserves_a_preexisting_archive_destination(backup_world, monkeypatch):
    from core import backups
    w = backup_world
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 28, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(backups, 'datetime', FixedDatetime)
    monkeypatch.setattr(backups.uuid, 'uuid4', lambda: SimpleNamespace(hex='a' * 32))
    saved = w.service.create(password=PASSWORD)
    path = w.service.archive_path(saved['id'])
    expected = path.read_bytes()
    w.store.save_message(w.sid, 'user', 'A later synthetic message')
    with pytest.raises(BackupError):
        w.service.create(password=PASSWORD)
    assert path.read_bytes() == expected
    assert not list(w.service.work.iterdir()) and not list(w.service.directory.glob('.altera-crypto-*.part'))


def test_failed_encrypted_restore_and_rollback_can_restart_recover_with_reentered_password_only(backup_world):
    w = backup_world
    saved = w.service.create(password=PASSWORD)
    newer(w)
    expected = contents(w)
    preview = w.service.preview(w.service.archive_path(saved['id']), password=PASSWORD)
    w.memory.fail = 2
    with pytest.raises(BackupError, match='automatic rollback failed'):
        w.service.apply(preview['token'], password=PASSWORD, recovery_password=RECOVERY_PASSWORD)
    assert w.service.gate.fault and w.service.journal.exists()
    with pytest.raises(StateBusy):
        w.service.gate.enter('synthetic chat')
    record = w.service._recovery_record()
    assert record['recovery_backup'].endswith('.alterabak')
    assert_no_credentials(w.service, PASSWORD, RECOVERY_PASSWORD)
    restarted = BackupService(w.root, w.store, w.memory, gate=StateGate())
    assert restarted.gate.fault
    path = restarted.archive_path(record['recovery_backup'])
    with pytest.raises(BackupError, match='could not be opened'):
        restarted.preview(path)
    recovery = restarted.preview(path, password=RECOVERY_PASSWORD)
    with pytest.raises(BackupError, match='could not be opened'):
        restarted.apply(recovery['token'])
    assert restarted.gate.fault and restarted.journal.exists()
    result = restarted.apply(recovery['token'], password=RECOVERY_PASSWORD)
    assert result['recovery_backup']['id'] == record['recovery_backup'], 'fault repair must use the existing recovery archive'
    assert contents(w) == expected and not restarted.gate.fault and not restarted.journal.exists()
    assert_no_credentials(restarted, PASSWORD, RECOVERY_PASSWORD)


def test_api_defaults_to_encrypted_and_generates_no_recovery_secret_unless_requested(backup_api, backup_world, monkeypatch):
    from core.routes import backups
    client, w = backup_api, backup_world
    def no_generation():
        pytest.fail('Recovery generation must be explicitly requested')
    monkeypatch.setattr(backups, 'generate_recovery_key', no_generation)
    invalid = client.post('/api/backups', json={})
    assert invalid.status_code == 400 and not w.service.list()
    response = client.post('/api/backups', json={'password': PASSWORD})
    assert response.status_code == 201
    private_headers(response)
    assert response.json['encrypted'] and response.json['id'].endswith('.alterabak') and 'recovery_key' not in response.json
    downloaded = client.get(response.json['download_url'])
    assert downloaded.status_code == 200 and downloaded.data.startswith(backup_crypto.MAGIC)
    assert downloaded.mimetype == 'application/octet-stream'
    private_headers(downloaded)


def test_versioned_mutations_and_advertised_contract_round_trip_authenticated_archives(backup_api, backup_world):
    client, w = backup_api, backup_world
    assert client.get('/api/backups').json['backup_api_version'] == 2
    saved = client.post('/api/backups/v2', json={'password': PASSWORD})
    assert saved.status_code == 201 and saved.json['encrypted']
    assert w.service.archive_path(saved.json['id']).read_bytes().startswith(backup_crypto.MAGIC)
    preview = client.post('/api/backups/v2/restore/preview', json={'backup_id': saved.json['id'], 'password': PASSWORD})
    assert preview.status_code == 200 and preview.json['encrypted']
    applied = client.post('/api/backups/v2/restore/apply', json={'token': preview.json['token'], 'confirm': 'restore',
        'password': PASSWORD, 'recovery_password': RECOVERY_PASSWORD})
    assert applied.status_code == 200 and applied.json['recovery_backup']['id'].endswith('.alterabak')
    private_headers(applied)


def test_api_recovery_key_is_returned_once_and_never_in_list_preview_or_restore_receipts(backup_api, backup_world, monkeypatch):
    from core.routes import backups
    client, w = backup_api, backup_world
    generated = []
    def generate():
        key = backup_crypto.generate_recovery_key()
        generated.append(key)
        return key
    monkeypatch.setattr(backups, 'generate_recovery_key', generate)
    response = client.post('/api/backups', json={'password': PASSWORD, 'include_recovery_key': True})
    assert response.status_code == 201 and len(generated) == 1
    key = response.json['recovery_key']
    assert key == generated[0] and response.json['recovery_enabled']
    listed = client.get('/api/backups')
    private_headers(listed)
    assert key not in listed.get_data(as_text=True) and listed.json['backups'][0]['counts'] == {}
    preview = client.post('/api/backups/restore/preview', json={'backup_id': response.json['id'], 'recovery_key': key})
    assert preview.status_code == 200 and preview.json['encrypted']
    private_headers(preview)
    assert key not in preview.get_data(as_text=True)
    refused = client.post('/api/backups/restore/apply', json={'token': preview.json['token'], 'confirm': 'restore', 'recovery_key': key})
    assert refused.status_code == 400, 'encrypted recovery is the API default and needs its new password'
    restored = client.post('/api/backups/restore/apply', json={'token': preview.json['token'], 'confirm': 'restore', 'recovery_key': key, 'recovery_password': RECOVERY_PASSWORD})
    assert restored.status_code == 200
    private_headers(restored)
    assert restored.json['recovery_backup']['id'].endswith('.alterabak')
    for secret in [PASSWORD, key, RECOVERY_PASSWORD]:
        assert secret not in json.dumps(restored.json, ensure_ascii=False)
    assert_no_credentials(w.service, PASSWORD, key, RECOVERY_PASSWORD)


@pytest.mark.parametrize('method', ['json', 'multipart'])
def test_api_preview_accepts_one_explicit_credential_and_rejects_missing_or_multiple(backup_api, backup_world, method):
    client, w = backup_api, backup_world
    key = backup_crypto.generate_recovery_key()
    saved = w.service.create(password=PASSWORD, recovery_key=key)
    if method == 'json':
        def request(**credentials):
            return client.post('/api/backups/restore/preview', json={'backup_id': saved['id'], **credentials})
    else:
        def request(**credentials):
            return client.post('/api/backups/restore/preview', data={'file': (BytesIO(w.service.archive_path(saved['id']).read_bytes()), 'upload.alterabak'), **credentials})
    for credentials in [{}, {'password': PASSWORD, 'recovery_key': key}]:
        response = request(**credentials)
        assert response.status_code == 400
        private_headers(response)
    response = request(password=PASSWORD)
    assert response.status_code == 200 and response.json['encrypted']
    private_headers(response)
    assert_preview_ciphertext_only(w.service, response.json, w.service.archive_path(saved['id']))


@pytest.mark.parametrize('malformed', ['wrong file field', 'duplicate password', 'unknown field'])
def test_api_multipart_field_names_and_multiplicity_are_strict(backup_api, backup_world, malformed):
    client, w = backup_api, backup_world
    saved = w.service.create(password=PASSWORD)
    upload = (BytesIO(w.service.archive_path(saved['id']).read_bytes()), 'upload.alterabak')
    if malformed == 'wrong file field':
        data = {'archive': upload, 'password': PASSWORD}
    elif malformed == 'duplicate password':
        data = MultiDict([('file', upload), ('password', PASSWORD), ('password', PASSWORD)])
    else:
        data = {'file': upload, 'password': PASSWORD, 'persist_password': 'true'}
    response = client.post('/api/backups/restore/preview', data=data)
    assert response.status_code == 400
    private_headers(response)
    assert not list(w.service.staging.iterdir()) and not list(w.service.work.iterdir())


def test_api_plain_zip_requires_explicit_opt_out_for_create_and_recovery(backup_api):
    client = backup_api
    for bad in [{'format': 'zip', 'password': PASSWORD}, {'format': 'zip', 'include_recovery_key': True}]:
        assert client.post('/api/backups', json=bad).status_code == 400
    created = client.post('/api/backups', json={'format': 'zip'})
    assert created.status_code == 201 and created.json['encrypted'] is False and created.json['id'].endswith('.zip')
    preview = client.post('/api/backups/restore/preview', json={'backup_id': created.json['id']})
    assert preview.status_code == 200
    missing_recovery_password = client.post('/api/backups/restore/apply', json={'token': preview.json['token'], 'confirm': 'restore'})
    assert missing_recovery_password.status_code == 400
    response = client.post('/api/backups/restore/apply', json={'token': preview.json['token'], 'confirm': 'restore', 'recovery_format': 'zip'})
    assert response.status_code == 200 and response.json['recovery_backup']['id'].endswith('.zip')
    private_headers(response)


def test_successful_token_retry_returns_its_app_receipt_without_another_unlock_or_restore(backup_world, monkeypatch):
    """Completed receipts are app context; unapplied restore tokens require unlock."""
    w = backup_world
    browser = {'dataset_epoch': w.store.dataset_epoch()['value'], 'preferences': {'altera_theme': 'light'},
               'drafts': [{'session_id': w.sid, 'message': 'Synthetic restored app draft'}]}
    saved = w.service.create(browser, password=PASSWORD)
    preview = w.service.preview(w.service.archive_path(saved['id']), password=PASSWORD)
    result = w.service.apply(preview['token'], password=PASSWORD, recovery_password=RECOVERY_PASSWORD)
    epoch = w.store.dataset_epoch()['value']
    archive_ids = [item['id'] for item in w.service.list()]
    messages = [message.content for message in w.store.get_messages(w.sid)]
    def no_work(*_, **__):
        pytest.fail('Completed receipt retry must not decrypt, snapshot or replace again')
    monkeypatch.setattr(backup_crypto, 'decrypt_file', no_work)
    monkeypatch.setattr(w.service, '_create', no_work)
    monkeypatch.setattr(w.memory, 'restore_snapshot', no_work)
    assert w.service.apply(preview['token']) == result
    assert result['browser_state']['drafts'] == browser['drafts']
    assert w.store.dataset_epoch()['value'] == epoch
    assert [item['id'] for item in w.service.list()] == archive_ids
    assert [message.content for message in w.store.get_messages(w.sid)] == messages
    assert_no_credentials(w.service, PASSWORD, RECOVERY_PASSWORD)
