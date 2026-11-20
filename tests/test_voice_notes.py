"""Durable voice notes use temporary SQLite/audio and a fake local transcriber."""
import hashlib
import io
import json
import shutil
import threading
import uuid
import wave

from flask import Flask
import pytest

from core import voice_notes
from core.request_security import install_request_security
from core.routes.voice_notes import bp
from core.voice_notes import VoiceNoteError, VoiceNoteService, inspect_audio
from test_companion_api import active_avatar, tmp_store, world  # noqa: F401


def recording(seconds=0.1, channels=1, rate=16000):
    output = io.BytesIO()
    with wave.open(output, 'wb') as audio:
        audio.setnchannels(channels)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(b'\x00\x00' * channels * int(rate * seconds))
    return output.getvalue()


def note_id():
    return 'vn_' + uuid.uuid4().hex


def test_review_context_does_not_construct_voice_service_or_reconcile_jobs(world, monkeypatch):
    sid = world.store.create_session('Synthetic recovery destination')
    app = Flask(__name__)
    app.register_blueprint(bp)
    monkeypatch.setattr(voice_notes, 'VoiceNoteService', lambda *args, **kwargs: pytest.fail('Context started voice service'))
    assert not hasattr(world.store, '_voice_note_service')
    result = app.test_client().get('/api/voice-notes/context?session_id=' + str(sid))
    assert result.status_code == 200
    assert result.json['session_id'] == sid and result.json['source_revision'] == 0
    assert not hasattr(world.store, '_voice_note_service')
    with world.store._connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM voice_notes').fetchone()[0] == 0
    for query in ('', '?session_id=1&session_id=2', '?session_id=1&extra=true', '?session_id=true'):
        assert app.test_client().get('/api/voice-notes/context' + query).status_code == 400
    assert app.test_client().get('/api/voice-notes/context?session_id=999999').status_code == 404


@pytest.fixture
def voices(world, monkeypatch):
    owner = active_avatar(world)['id']
    sid = world.store.ensure_avatar_session(owner, 'Mara')
    epoch = world.store.dataset_epoch()['value']
    calls = []
    def local_transcriber(content, **kwargs):
        calls.append((content, kwargs))
        assert kwargs == {'offline_only': True}
        return {'text': 'Reviewed words from local audio.', 'language': 'en'}
    service = VoiceNoteService(world.store, world.tmp / 'voice_notes', transcriber=local_transcriber, sync=True)
    monkeypatch.setattr(world.store, '_voice_note_service', service, raising=False)
    app = Flask(__name__)
    app.register_blueprint(bp)
    install_request_security(app)
    return type('VoiceWorld', (), {'service': service, 'store': world.store, 'sid': sid, 'owner': owner,
        'epoch': epoch, 'calls': calls, 'client': app.test_client(), 'world': world})()


def upload(v, identifier=None, content=None):
    context = v.service.context(v.sid)
    return v.service.upload(identifier or note_id(), v.sid, v.epoch, io.BytesIO(content or recording()),
                            source_revision=context['source_revision'], recall_after=context['recall_after'], avatar_id=context['avatar_id'])


def ready(v):
    item = upload(v)
    item = v.service.transcribe(item['note_id'], v.epoch)
    return v.service.review(item['note_id'], v.epoch, item['revision'])


def test_upload_is_durable_bounded_and_deduped_without_transcribing_or_sending(voices):
    v = voices
    identifier, content = note_id(), recording()
    first = upload(v, identifier, content)
    assert first['status'] == 'upload' and first['upload_complete'] and first['transcript'] == ''
    assert first['audio_sha256'] == hashlib.sha256(content).hexdigest()
    assert first['duration_seconds'] == pytest.approx(0.1)
    assert first['source_revision'] == 0 and first['recall_after'] == 0
    assert v.calls == [] and v.store.get_messages(v.sid) == []
    assert upload(v, identifier, content) == first
    assert v.service.audio(identifier)[0].read_bytes() == content
    with pytest.raises(VoiceNoteError, match='different audio'):
        upload(v, identifier, recording(0.2))
    assert v.service.get(identifier)['audio_sha256'] == first['audio_sha256']
    other_sid = v.store.create_session('Other')
    with pytest.raises(VoiceNoteError, match='different recording context'):
        v.service.upload(identifier, other_sid, v.epoch, io.BytesIO(content), source_revision=0, recall_after=0, avatar_id='')
    assert len(v.service.list(v.sid)) == 1
    assert 'input.wav' not in json.dumps(v.service.get(identifier))


def test_corrupted_recording_is_not_played_transcribed_or_acknowledged_on_retry(voices):
    v = voices
    original = recording()
    item = upload(v, content=original)
    path = v.service.audio(item['note_id'])[0]
    damaged = b'x' * len(original)
    path.write_bytes(damaged)

    result = v.service.get(item['note_id'])
    assert result['audio_url'] is None and 'damaged' in result['audio_warning']
    response = v.client.get(f"/api/voice-notes/{item['note_id']}/audio")
    assert response.status_code == 409 and 'damaged' in response.json['error']
    with pytest.raises(VoiceNoteError, match='damaged'):
        v.service.transcribe(item['note_id'], v.epoch)
    with pytest.raises(VoiceNoteError, match='damaged'):
        upload(v, item['note_id'], original)
    assert v.calls == [] and path.read_bytes() == damaged


def test_corrupted_recording_after_job_admission_never_reaches_transcriber(voices, monkeypatch):
    v = voices
    pending = []
    monkeypatch.setattr('core.state_gate.start_job', lambda target, args, **kwargs: pending.append((target, args)))
    v.service.sync = False
    item = upload(v)
    assert v.service.transcribe(item['note_id'], v.epoch)['status'] == 'transcribing'
    path = v.service.root / item['note_id'] / 'input.wav'
    path.write_bytes(b'x' * item['audio_bytes'])
    target, args = pending[0]
    target(*args)
    result = v.service.get(item['note_id'])
    assert result['status'] == 'failed' and result['error_stage'] == 'transcribe'
    assert 'damaged' in result['error'] and 'damaged' in result['audio_warning']
    assert v.calls == [] and path.read_bytes() == b'x' * item['audio_bytes']


def test_real_parser_rejects_malformed_truncated_playlist_duration_channels_and_rate(tmp_path):
    cases = [b'not audio', b'#EXTM3U\nfile:///private.wav', recording()[:60], recording(0.1, channels=3), recording(0.1, rate=192000)]
    for i, content in enumerate(cases):
        path = tmp_path / f'bad-{i}.wav'
        path.write_bytes(content)
        with pytest.raises(VoiceNoteError):
            inspect_audio(path)
    long = tmp_path / 'long.wav'
    long.write_bytes(recording(301, rate=8000))
    with pytest.raises(VoiceNoteError, match='five minutes'):
        inspect_audio(long)
    valid = tmp_path / 'disguised.mp3'
    valid.write_bytes(recording())
    assert inspect_audio(valid)['ext'] == 'wav'


def test_bad_upload_failure_is_retryable_and_never_acknowledges_missing_audio(voices, monkeypatch):
    v = voices
    identifier = note_id()
    with pytest.raises(VoiceNoteError, match='recognized'):
        upload(v, identifier, b'not audio')
    assert v.service.get(identifier)['status'] == 'failed'
    assert not v.service.get(identifier)['upload_complete']
    assert upload(v, identifier)['status'] == 'upload'
    monkeypatch.setattr(voice_notes, 'MAX_AUDIO_BYTES', 60)
    another = note_id()
    with pytest.raises(VoiceNoteError) as error:
        upload(v, another)
    assert error.value.status == 413
    assert not v.service.get(another)['upload_complete']
    assert not list(v.service.root.rglob('.upload-*'))


def test_transcription_is_explicit_cached_local_only_and_revision_review_gates_send(voices):
    v = voices
    item = upload(v)
    with pytest.raises(VoiceNoteError, match='Review'):
        v.service.prepare_send(item['note_id'], v.sid, v.epoch, 0)
    item = v.service.transcribe(item['note_id'], v.epoch)
    assert item['status'] == 'review' and item['revision'] == 1 and len(v.calls) == 1
    assert v.store.get_messages(v.sid) == []
    with pytest.raises(VoiceNoteError, match='Review'):
        v.service.prepare_send(item['note_id'], v.sid, v.epoch, item['revision'])
    item = v.service.review(item['note_id'], v.epoch, item['revision'])
    assert item['status'] == 'ready'
    item = v.service.edit(item['note_id'], v.epoch, 'Corrected café words', item['revision'])
    assert item['status'] == 'review' and item['reviewed_revision'] == -1 and item['revision'] == 2
    with pytest.raises(VoiceNoteError):
        v.service.review(item['note_id'], v.epoch, 1)
    v.service.review(item['note_id'], v.epoch, item['revision'])
    send = v.service.prepare_send(item['note_id'], v.sid, v.epoch, item['revision'])
    assert send['message'] == 'Corrected café words' and send['request_id'] == 'voice_' + item['note_id']
    assert v.service.prepare_send(item['note_id'], v.sid, v.epoch, item['revision']) == send
    with pytest.raises(VoiceNoteError):
        v.service.edit(item['note_id'], v.epoch, 'Changed after reservation', item['revision'])
    assert v.service.abort_send(item['note_id'])['status'] == 'ready'


@pytest.mark.parametrize(('voice_config', 'expected_model'), [({'stt_model': 'tiny'}, 'tiny'), ({}, 'base')])
def test_ordinary_voice_note_uses_configured_offline_speech_model(voices, monkeypatch, voice_config, expected_model):
    import core.config
    import core.stt
    calls = []
    monkeypatch.setattr(core.config, 'get_config', lambda: {'voice': voice_config})
    monkeypatch.setattr(core.stt, 'transcribe_from_bytes',
                        lambda audio, **kwargs: calls.append((audio, kwargs)) or {'text': 'Synthetic words', 'language': 'en'})
    voices.service.transcriber = None
    item = upload(voices)
    result = voices.service.transcribe(item['note_id'], voices.epoch)
    assert result['status'] == 'review' and result['transcript'] == 'Synthetic words'
    assert calls == [(recording(), {'model_size': expected_model, 'offline_only': True})]


def test_unavailable_local_transcription_reports_503_without_auto_retry(voices):
    v = voices
    item = upload(v)
    v.service.transcriber = lambda *args, **kwargs: {'text': '', 'error': 'weights not cached'}
    with pytest.raises(VoiceNoteError) as error:
        v.service.transcribe(item['note_id'], v.epoch)
    assert error.value.status == 503
    failed = v.service.get(item['note_id'])
    assert failed['status'] == 'failed' and failed['error_stage'] == 'transcribe'
    assert 'cached Whisper weights' in failed['error']
    assert not v.service.jobs


def test_async_transcription_retries_are_deduped_and_stale_native_job_cannot_publish(voices, monkeypatch):
    v = voices
    pending = []
    monkeypatch.setattr('core.state_gate.start_job', lambda target, args, **kwargs: pending.append((target, args)))
    v.service.sync = False
    item = upload(v)
    assert v.service.transcribe(item['note_id'], v.epoch)['status'] == 'transcribing'
    assert v.service.transcribe(item['note_id'], v.epoch)['status'] == 'transcribing'
    assert len(pending) == 1 and v.calls == []
    v.service.invalidate_owner(v.owner)
    with pytest.raises(VoiceNoteError, match='Wait'):
        v.service.delete(item['note_id'])
    target, args = pending[0]
    target(*args)
    assert v.service.get(item['note_id'])['status'] == 'stale'
    assert v.service.get(item['note_id'])['transcript'] == '' and not v.service.jobs and v.calls == []


def insert_turn(v, item, status='saved', *, error='', audio_url=None):
    user_id = v.store.save_message(v.sid, 'user', item['transcript'])
    assistant_id = v.store.save_message(v.sid, 'assistant', 'A saved response') if status == 'saved' else None
    with v.service.connection() as conn:
        conn.execute('INSERT INTO chat_turns(request_id,session_id,fingerprint,process_id,status,user_message_id,assistant_message_id,error) '
                     'VALUES(?,?,?,?,?,?,?,?)', (item['request_id'], v.sid, 'fake-fingerprint', voice_notes.PROCESS_ID, status, user_id, assistant_id, error))
        if audio_url:
            conn.execute('INSERT INTO chat_turn_events(request_id,event) VALUES(?,?)', (item['request_id'], json.dumps({'status': 'audio', 'audio_url': audio_url})))
    return {'request_id': item['request_id'], 'session_id': v.sid}


def test_send_reconciles_one_existing_turn_and_retains_reply_audio_past_cache_deletion(voices, monkeypatch):
    from core import tts
    v = voices
    item = ready(v)
    send = v.service.prepare_send(item['note_id'], v.sid, v.epoch, item['revision'])
    cache = v.world.tmp / 'tts-cache'
    cache.mkdir()
    clip = cache / 'generated.wav'
    clip.write_bytes(recording())
    monkeypatch.setattr(tts, 'AUDIO_CACHE_DIR', str(cache))
    turn = insert_turn(v, item, audio_url='/static/audio/tts/generated.wav')
    assert v.service._raw(item['note_id'])['user_message_id'] is None
    v.service.settle_turn(item['request_id'])
    completed = v.service._raw(item['note_id'])
    assert completed['user_message_id'] is not None and completed['assistant_message_id'] is not None
    retained = json.loads(completed['reply_audio'])
    assert len(retained) == 1
    filename = retained[0]['filename']
    clip.unlink()
    # No note.get/mark_submitted browser polling before the source cache disappears.
    assert v.service.audio(item['note_id'], filename)[0].read_bytes() == recording()
    sent = v.service.mark_submitted(item['note_id'], turn)
    assert sent['status'] == 'sent' and sent['user_message_id'] and sent['assistant_message_id']
    assert v.service.get(item['note_id'])['reply_audio'] == sent['reply_audio']
    assert v.service.audio(item['note_id'], filename)[0].read_bytes() == recording()
    assert v.service.prepare_send(item['note_id'], v.sid, v.epoch, item['revision']) == send
    assert len(v.store.get_messages(v.sid)) == 2
    assert v.service.abort_send(item['note_id'])['status'] == 'sent'


@pytest.mark.parametrize('keep_cache', [False, True])
def test_damaged_retained_reply_is_not_reported_or_served(voices, monkeypatch, keep_cache):
    from core import tts
    v = voices
    item = ready(v)
    v.service.prepare_send(item['note_id'], v.sid, v.epoch, item['revision'])
    cache = v.world.tmp / 'tts-corruption'
    cache.mkdir()
    source = cache / 'reply.wav'
    source.write_bytes(recording())
    monkeypatch.setattr(tts, 'AUDIO_CACHE_DIR', str(cache))
    insert_turn(v, item, audio_url='/static/audio/tts/reply.wav')
    v.service.settle_turn(item['request_id'])
    stored = json.loads(v.service._raw(item['note_id'])['reply_audio'])[0]['filename']
    path = v.service._folder(item['note_id']) / stored
    damaged = b'x' * len(recording())  # Preserve size so metadata alone cannot detect corruption.
    path.write_bytes(damaged)
    if not keep_cache:
        source.unlink()

    with pytest.raises(VoiceNoteError, match='damaged'):
        v.service.audio(item['note_id'], stored)
    response = v.client.get(f"/api/voice-notes/{item['note_id']}/replies/{stored}")
    assert response.status_code == 409 and 'damaged' in response.json['error']
    result = v.service.get(item['note_id'])
    assert result['status'] == 'sent' and result['assistant_message_id']
    assert result['reply_audio'] == [] and result['reply_audio_warning']
    assert path.read_bytes() == damaged  # Preserve the damaged original for inspection.
    result = v.service.get(item['note_id'])  # A later refresh must not overwrite the damaged file.
    assert result['reply_audio'] == [] and result['reply_audio_warning']
    assert path.read_bytes() == damaged
    if keep_cache:
        assert source.read_bytes() == recording()
        path.unlink()
        source.write_bytes(recording(0.2))  # Reused source URLs cannot change a saved reply's identity.
        result = v.service.get(item['note_id'])
        assert result['reply_audio'] == [] and result['reply_audio_warning']


def test_missing_retained_reply_recovers_only_the_same_cached_content(voices, monkeypatch):
    from core import tts
    v = voices
    item = ready(v)
    v.service.prepare_send(item['note_id'], v.sid, v.epoch, item['revision'])
    cache = v.world.tmp / 'tts-recovery'
    cache.mkdir()
    source = cache / 'reply.wav'
    source.write_bytes(recording())
    monkeypatch.setattr(tts, 'AUDIO_CACHE_DIR', str(cache))
    insert_turn(v, item, audio_url='/static/audio/tts/reply.wav')
    v.service.settle_turn(item['request_id'])
    stored = json.loads(v.service._raw(item['note_id'])['reply_audio'])[0]['filename']
    path = v.service._folder(item['note_id']) / stored
    path.unlink()
    source.write_bytes(recording(0.2))
    result = v.service.get(item['note_id'])
    assert result['reply_audio'] == [] and result['reply_audio_warning'] and not path.exists()
    source.write_bytes(recording())
    result = v.service.get(item['note_id'])
    assert [reply['filename'] for reply in result['reply_audio']] == [stored]
    assert not result['reply_audio_warning'] and path.read_bytes() == recording()


def test_stale_voice_note_warns_if_its_retained_reply_is_damaged(voices, monkeypatch):
    from core import tts
    v = voices
    item = ready(v)
    v.service.prepare_send(item['note_id'], v.sid, v.epoch, item['revision'])
    cache = v.world.tmp / 'tts-stale'
    cache.mkdir()
    (cache / 'reply.wav').write_bytes(recording())
    monkeypatch.setattr(tts, 'AUDIO_CACHE_DIR', str(cache))
    insert_turn(v, item, audio_url='/static/audio/tts/reply.wav')
    v.service.settle_turn(item['request_id'])
    stored = json.loads(v.service._raw(item['note_id'])['reply_audio'])[0]['filename']
    (v.service._folder(item['note_id']) / stored).write_bytes(b'damaged')
    v.service.invalidate_owner(v.owner)
    result = v.service.get(item['note_id'])
    assert result['status'] == 'stale' and result['reply_audio'] == []
    assert result['reply_audio_warning']


def test_retained_reply_listing_respects_total_size_limit(voices, monkeypatch):
    from core import tts
    v = voices
    item = ready(v)
    v.service.prepare_send(item['note_id'], v.sid, v.epoch, item['revision'])
    cache = v.world.tmp / 'tts-limit'
    cache.mkdir()
    (cache / 'one.wav').write_bytes(recording())
    (cache / 'two.wav').write_bytes(recording(0.2))
    monkeypatch.setattr(tts, 'AUDIO_CACHE_DIR', str(cache))
    insert_turn(v, item, audio_url='/static/audio/tts/one.wav')
    with v.service.connection() as conn:
        conn.execute('INSERT INTO chat_turn_events(request_id,event) VALUES(?,?)',
                     (item['request_id'], json.dumps({'status': 'audio', 'audio_url': '/static/audio/tts/two.wav'})))
    v.service.settle_turn(item['request_id'])
    saved = json.loads(v.service._raw(item['note_id'])['reply_audio'])
    assert len(saved) == 2
    monkeypatch.setattr(voice_notes, 'MAX_REPLY_BYTES', len(recording(0.2)))
    result = v.service.get(item['note_id'])
    assert len(result['reply_audio']) == 1 and result['reply_audio_warning']
    assert len(json.loads(v.service._raw(item['note_id'])['reply_audio'])) == 2


def test_turn_completion_retains_audio_with_no_browser_or_note_polling(voices, monkeypatch):
    from core import tts
    from core.turns import CURRENT_TURN, get_turn_service
    v = voices
    item = ready(v)
    prepared = v.service.prepare_send(item['note_id'], v.sid, v.epoch, item['revision'])
    cache = v.world.tmp / 'tts-disconnected'
    cache.mkdir()
    clip = cache / 'disconnected.wav'
    clip.write_bytes(recording())
    monkeypatch.setattr(tts, 'AUDIO_CACHE_DIR', str(cache))
    settled = threading.Event()
    original = v.service.settle_turn
    def settle(request_id):
        original(request_id)
        settled.set()
    monkeypatch.setattr(v.service, 'settle_turn', settle)
    def produce(session_id):
        turn = CURRENT_TURN.get()
        turn.update(user_message_id=v.store.save_message(session_id, 'user', item['transcript']))
        yield {'response': 'Reply with retained speech'}
        turn.update(assistant_message_id=v.store.save_message(session_id, 'assistant', 'Reply with retained speech'))
        yield {'status': 'audio', 'audio_url': '/static/audio/tts/disconnected.wav'}
    turns = get_turn_service(v.store)
    turns.submit(prepared['request_id'], v.sid, item['transcript'], False, produce, v.client.application, dataset_epoch=v.epoch)
    assert settled.wait(5), 'The reply worker must retain attachments without a browser request'
    with turns.changed:
        assert turns.changed.wait_for(lambda: prepared['request_id'] not in turns.jobs, timeout=5)
    clip.unlink()
    raw = v.service._raw(item['note_id'])
    assert raw['status'] == 'sent' and raw['reply_audio_warning'] == ''
    filename = json.loads(raw['reply_audio'])[0]['filename']
    assert v.service.audio(item['note_id'], filename)[0].read_bytes() == recording()


def test_text_turn_does_not_initialize_or_reconcile_unrelated_voice_notes(voices, monkeypatch):
    from core.turns import CURRENT_TURN, get_turn_service
    v = voices
    item = ready(v)
    before = v.service._raw(item['note_id'])
    monkeypatch.delattr(v.store, '_voice_note_service')
    calls = []
    monkeypatch.setattr(voice_notes, 'get_voice_note_service', lambda *args: calls.append(args))

    def produce(session_id):
        turn = CURRENT_TURN.get()
        turn.update(user_message_id=v.store.save_message(session_id, 'user', 'An ordinary text message'))
        yield {'response': 'A text reply'}
        turn.update(assistant_message_id=v.store.save_message(session_id, 'assistant', 'A text reply'))

    turns = get_turn_service(v.store)
    request_id = 'ordinary-text-turn-123'
    turns.submit(request_id, v.sid, 'An ordinary text message', False, produce, v.client.application, dataset_epoch=v.epoch)
    with turns.changed:
        assert turns.changed.wait_for(lambda: request_id not in turns.jobs, timeout=5)
    assert turns.get(request_id)['status'] == 'saved'
    assert calls == [] and not hasattr(v.store, '_voice_note_service')
    assert v.service._raw(item['note_id']) == before


def test_linked_turn_can_initialize_voice_service_and_retain_audio(voices, monkeypatch):
    from core import paths, tts
    v = voices
    item = ready(v)
    v.service.prepare_send(item['note_id'], v.sid, v.epoch, item['revision'])
    cache = v.world.tmp / 'tts-new-service'
    cache.mkdir()
    clip = cache / 'linked.wav'
    clip.write_bytes(recording())
    monkeypatch.setattr(tts, 'AUDIO_CACHE_DIR', str(cache))
    insert_turn(v, item, audio_url='/static/audio/tts/linked.wav')
    monkeypatch.delattr(v.store, '_voice_note_service')
    monkeypatch.setattr(paths, 'data_path', lambda name: v.service.root if name == 'voice_notes' else v.world.tmp / name)
    voice_notes.settle_linked_turn(v.store, item['request_id'])
    service = v.store._voice_note_service
    saved = service._raw(item['note_id'])
    assert saved['status'] == 'sent' and len(json.loads(saved['reply_audio'])) == 1
    clip.unlink()
    assert service.audio(item['note_id'], json.loads(saved['reply_audio'])[0]['filename'])[0].read_bytes() == recording()


def test_settle_turn_keeps_saved_reply_truthful_when_audio_copy_is_unavailable(voices):
    v = voices
    item = ready(v)
    v.service.prepare_send(item['note_id'], v.sid, v.epoch, item['revision'])
    insert_turn(v, item, audio_url='/static/audio/tts/missing.wav')
    v.service.settle_turn(item['request_id'])
    raw = v.service._raw(item['note_id'])
    assert raw['status'] == 'sent' and raw['assistant_message_id']
    assert raw['reply_audio'] == '[]' and raw['reply_audio_warning']


def test_failed_turn_stays_one_immutable_request_and_replay_never_generates(voices):
    v = voices
    item = ready(v)
    first = v.service.prepare_send(item['note_id'], v.sid, v.epoch, item['revision'])
    turn = insert_turn(v, item, status='failed', error='A truthful turn failure')
    failed = v.service.mark_submitted(item['note_id'], turn)
    assert failed['status'] == 'failed' and failed['error_stage'] == 'send'
    assert v.service.prepare_send(item['note_id'], v.sid, v.epoch, item['revision']) == first
    assert v.service.abort_send(item['note_id'])['status'] == 'failed'
    assert len(v.store.get_messages(v.sid)) == 1


def test_restart_recovery_never_transcribes_or_submits_automatically(voices):
    v = voices
    transcribing = upload(v)
    reserved = ready(v)
    v.service.prepare_send(reserved['note_id'], v.sid, v.epoch, reserved['revision'])
    with v.service.connection() as conn:
        conn.execute("UPDATE voice_notes SET status='transcribing',process_id='old-process' WHERE note_id=?", (transcribing['note_id'],))
        conn.execute("UPDATE voice_notes SET process_id='old-process' WHERE note_id=?", (reserved['note_id'],))
    calls_before = len(v.calls)
    recovered = VoiceNoteService(v.store, v.service.root, transcriber=lambda *args, **kwargs: pytest.fail('No automatic inference'), sync=True)
    assert recovered.get(transcribing['note_id'])['status'] == 'failed'
    assert recovered.get(reserved['note_id'])['status'] == 'ready'
    assert len(v.calls) == calls_before and v.store.get_messages(v.sid) == []


@pytest.mark.parametrize('change', ['epoch', 'revision', 'floor', 'owner'])
def test_context_changes_prevent_sending_or_retranscribing_old_notes(voices, change):
    v = voices
    item = ready(v)
    if change == 'epoch':
        v.store.set_key('dataset_epoch', {'value': 'new-dataset'})
    elif change == 'revision':
        v.store.set_key(f'knowledge_dirty:{v.sid}', {'revision': 1, 'pending': True})
    elif change == 'floor':
        v.store.set_key('knowledge_recall:' + v.owner, {'through': 100})
    else:
        v.store.set_session_avatar(v.sid, 'another-owner')
    with pytest.raises(VoiceNoteError):
        v.service.prepare_send(item['note_id'], v.sid, v.epoch, item['revision'])
    assert v.store.get_messages(v.sid) == []


def test_deletion_failure_keeps_discoverable_retry_then_redacts_and_blocks_resurrection(voices, monkeypatch):
    v = voices
    item = ready(v)
    original = shutil.rmtree
    def fail(*args, **kwargs):
        raise OSError('filesystem busy')
    monkeypatch.setattr(voice_notes.shutil, 'rmtree', fail)
    with pytest.raises(VoiceNoteError, match='could not be removed'):
        v.service.delete(item['note_id'])
    assert v.service._raw(item['note_id'])['transcript'] and (v.service.root / ('.delete-' + item['note_id'])).is_dir()
    assert v.service._raw(item['note_id'])['status'] == 'stale'
    with pytest.raises(VoiceNoteError):
        v.service.prepare_send(item['note_id'], v.sid, v.epoch, item['revision'])
    monkeypatch.setattr(voice_notes.shutil, 'rmtree', original)
    v.service.delete(item['note_id'])
    assert not (v.service.root / ('.delete-' + item['note_id'])).exists()
    raw = v.service._raw(item['note_id'])
    assert raw['error_stage'] == 'deleted' and not raw['transcript'] and not raw['upload_complete']
    assert v.service.list(v.sid) == []
    with pytest.raises(VoiceNoteError) as error:
        upload(v, item['note_id'])
    assert error.value.status == 410
    with pytest.raises(VoiceNoteError):
        v.service.get(item['note_id'])


def test_delete_refuses_sending_and_path_traversal(voices):
    v = voices
    item = ready(v)
    v.service.prepare_send(item['note_id'], v.sid, v.epoch, item['revision'])
    with pytest.raises(VoiceNoteError, match='Wait'):
        v.service.delete(item['note_id'])
    for invalid in ('../secret', 'vn_' + 'A' * 32, 'vn_' + 'f' * 32 + '/x'):
        with pytest.raises(VoiceNoteError):
            v.service.get(invalid)
    with pytest.raises(VoiceNoteError):
        v.service.audio(item['note_id'], '../secret.wav')


def test_routes_preserve_owner_reads_origin_guards_audio_ranges_and_no_store(voices):
    v = voices
    identifier = note_id()
    data = {'note_id': identifier, 'session_id': str(v.sid), 'dataset_epoch': v.epoch,
            'source_revision': '0', 'recall_after': '0', 'avatar_id': v.owner,
            'audio': (io.BytesIO(recording()), '../../fake.mp3', 'text/html')}
    response = v.client.post('/api/voice-notes', data=data)
    assert response.status_code == 201 and response.json['note']['status'] == 'upload'
    path = '/api/voice-notes/' + identifier
    assert v.client.get('/api/voice-notes?session_id=' + str(v.sid)).json['notes'][0]['note_id'] == identifier
    assert v.client.get(path + '?session_id=9999').status_code == 404
    audio = v.client.get(path + '/audio', headers={'Range': 'bytes=0-9'})
    assert audio.status_code == 206 and audio.mimetype == 'audio/wav' and audio.data == recording()[:10]
    assert audio.headers['Cache-Control'] == 'no-store' and audio.headers['X-Content-Type-Options'] == 'nosniff'
    audio.close()
    assert v.client.post(path + '/transcribe', json={'dataset_epoch': v.epoch}, headers={'Origin': 'http://evil.example'}).status_code == 403
    response = v.client.post(path + '/transcribe', json={'dataset_epoch': v.epoch})
    assert response.status_code == 200 and response.json['note']['status'] == 'review'
    revision = response.json['note']['revision']
    assert v.client.post(path + '/review', json={'dataset_epoch': v.epoch, 'revision': revision}).json['note']['status'] == 'ready'
    assert v.client.put(path, json={'dataset_epoch': v.epoch, 'revision': revision, 'transcript': 'Edited words'}).json['note']['status'] == 'review'
    assert v.client.delete(path, json={'dataset_epoch': v.epoch}).status_code == 400
    assert v.client.delete(path, json={'dataset_epoch': v.epoch, 'confirm': 'delete-voice-note'}).status_code == 200
    assert v.client.get(path).status_code == 404


@pytest.mark.parametrize('change', ['floor', 'revision', 'owner'])
def test_captured_context_cannot_upload_queued_audio_after_forget_edit_or_reassignment(voices, change):
    v = voices
    context = v.client.get('/api/voice-notes/context?session_id=' + str(v.sid)).json
    assert context == {'session_id': v.sid, 'avatar_id': v.owner, 'dataset_epoch': v.epoch, 'source_revision': 0, 'recall_after': 0}
    if change == 'floor':
        v.store.set_key('knowledge_recall:' + v.owner, {'through': 100})
    elif change == 'revision':
        v.store.set_key(f'knowledge_dirty:{v.sid}', {'revision': 1, 'pending': True})
    else:
        v.store.set_session_avatar(v.sid, 'different-owner')
    identifier = note_id()
    response = v.client.post('/api/voice-notes', data={
        **{key: str(value) for key, value in context.items()}, 'note_id': identifier,
        'audio': (io.BytesIO(recording()), 'recording.webm')})
    assert response.status_code == 409
    assert v.service._raw(identifier) is None and not v.service._folder(identifier).exists() and not v.calls


def test_upload_requires_capture_context_and_rechecks_it_after_parser(voices):
    v = voices
    identifier = note_id()
    response = v.client.post('/api/voice-notes', data={'session_id': str(v.sid), 'dataset_epoch': v.epoch,
        'note_id': identifier, 'audio': (io.BytesIO(recording()), 'recording.wav')})
    assert response.status_code == 400 and not v.service._folder(identifier).exists()
    original = v.service.parser
    def changing_parser(path):
        result = original(path)
        v.store.set_key('knowledge_recall:' + v.owner, {'through': 2})
        return result
    v.service.parser = changing_parser
    with pytest.raises(VoiceNoteError, match='context changed'):
        upload(v, identifier)
    assert not v.service._raw(identifier)['upload_complete']
    assert v.service._raw(identifier)['status'] == 'stale'
    assert not list(v.service._folder(identifier).glob('input.*'))


def test_chat_stream_sends_reviewed_voice_note_once_and_ignores_payload_substitution(voices, monkeypatch, altera_config):
    from core.routes import core_api
    from core.turns import CURRENT_TURN
    import core.emotion
    v = voices
    app = v.client.application
    app.register_blueprint(core_api.bp)
    calls = []
    class FakeEngine:
        def send_message(self, message, session_id, **kwargs):
            calls.append(message)
            turn = CURRENT_TURN.get()
            turn.update(user_message_id=v.store.save_message(session_id, 'user', message))
            yield 'One saved reply.'
            turn.update(assistant_message_id=v.store.save_message(session_id, 'assistant', 'One saved reply.'))
            yield None
    monkeypatch.setattr(core_api, '_get_chat_engine', lambda: FakeEngine())
    monkeypatch.setattr(core.emotion, 'analyze_and_store_emotion', lambda *args, **kwargs: None)
    altera_config.write_text('voice:\n  tts_enabled: false\n', encoding='utf-8')
    item = upload(v)
    payload = {'voice_note_id': item['note_id'], 'session_id': v.sid, 'dataset_epoch': v.epoch,
               'reviewed_revision': 1, 'message': 'UNREVIEWED SUBSTITUTION', 'request_id': 'untrusted-request-id'}
    assert v.client.post('/api/chat/stream', json=payload).status_code == 409 and calls == []
    item = v.service.transcribe(item['note_id'], v.epoch)
    v.service.review(item['note_id'], v.epoch, item['revision'])
    def decoded(response):
        return [json.loads(line[6:]) for line in response.get_data(as_text=True).splitlines() if line.startswith('data: ')]
    first = decoded(v.client.post('/api/chat/stream', json=payload))
    second = decoded(v.client.post('/api/chat/stream', json=payload))
    assert first[-1]['status'] == second[-1]['status'] == 'saved'
    assert first[-1]['request_id'] == second[-1]['request_id'] == item['request_id']
    assert calls == [item['transcript']]
    assert [m.content for m in v.store.get_messages(v.sid)] == [item['transcript'], 'One saved reply.']
    assert v.service.get(item['note_id'])['status'] == 'sent'


def test_chat_submission_rejection_releases_reviewed_note_reservation(voices, monkeypatch):
    from core.routes import core_api
    from core.turns import PROCESS_ID, get_turn_service
    v = voices
    v.client.application.register_blueprint(core_api.bp)
    monkeypatch.setattr(core_api, '_get_chat_engine', lambda: object())
    item = ready(v)
    turns = get_turn_service(v.store)
    with turns.connection() as conn:
        conn.execute("INSERT INTO chat_turns(request_id,session_id,fingerprint,process_id,status) VALUES('other-active-request',?,'other',?,'streaming')", (v.sid, PROCESS_ID))
    response = v.client.post('/api/chat/stream', json={'voice_note_id': item['note_id'], 'session_id': v.sid,
        'dataset_epoch': v.epoch, 'reviewed_revision': item['revision']})
    assert response.status_code == 409
    assert v.service.get(item['note_id'])['status'] == 'ready'
    assert turns.get(item['request_id']) is None


@pytest.mark.parametrize('link', ['user_message_id', 'assistant_message_id'])
@pytest.mark.parametrize('polled', [False, True])
def test_clear_message_removes_linked_audio_and_transcript_with_retryable_storage_failure(voices, monkeypatch, link, polled):
    v = voices
    item = ready(v)
    v.service.prepare_send(item['note_id'], v.sid, v.epoch, item['revision'])
    turn = insert_turn(v, item)
    if polled:
        v.service.mark_submitted(item['note_id'], turn)
    reply_filename = 'reply-' + hashlib.sha256(recording()).hexdigest() + '.wav'
    (v.service._folder(item['note_id']) / reply_filename).write_bytes(recording())
    v.service._update(item['note_id'], reply_audio=json.dumps([{'filename': reply_filename,
        'original_url': '/static/audio/tts/reply.wav'}]))
    with v.service.connection() as conn:
        message_id = conn.execute('SELECT ' + link + ' FROM chat_turns WHERE request_id=?', (item['request_id'],)).fetchone()[0]
    original = shutil.rmtree
    monkeypatch.setattr(voice_notes.shutil, 'rmtree', lambda *args, **kwargs: (_ for _ in ()).throw(OSError('busy')))
    with pytest.raises(VoiceNoteError, match='could not be removed'):
        v.service.clear_message(message_id)
    assert v.service._raw(item['note_id'])['transcript'] and v.store.get_message(message_id)
    monkeypatch.setattr(voice_notes.shutil, 'rmtree', original)
    v.service.clear_message(message_id)
    assert not v.service._folder(item['note_id']).exists()
    assert v.service._raw(item['note_id'])['error_stage'] == 'deleted'
    assert v.service._raw(item['note_id'])['transcript'] == ''
    assert v.service.list(v.sid) == []
    v.service.invalidate_owner(v.owner)
    v.service.invalidate_session(v.sid)
    assert v.service._raw(item['note_id'])['error_stage'] == 'deleted'
