"""Attachments participate in real backup and message/session deletion paths."""
import io
import os

import pytest

from core.voice_notes import VoiceNoteError, VoiceNoteService
from test_backups import backup_world  # noqa: F401
from test_companion_api import tmp_store, world  # noqa: F401
from test_voice_notes import note_id, recording, upload, voices  # noqa: F401


def test_backup_restores_voice_bytes_and_metadata_but_never_resumes_work(backup_world):
    w = backup_world
    service = VoiceNoteService(w.store, w.root / 'data' / 'voice_notes')
    context, content = service.context(w.sid), recording()
    item = service.upload(note_id(), w.sid, context['dataset_epoch'], io.BytesIO(content),
        source_revision=context['source_revision'], recall_after=context['recall_after'], avatar_id=context['avatar_id'])
    saved = w.service.create()
    preview = w.service.preview(w.service.archive_path(saved['id']))
    service.delete(item['note_id'])
    assert not service._folder(item['note_id']).exists()
    result = w.service.apply(preview['token'])
    restored = service.get(item['note_id'])
    assert restored['status'] == 'stale'
    assert restored['transcript'] == '' and restored['upload_complete']
    assert restored['audio_sha256'] == item['audio_sha256']
    assert service.audio(item['note_id'])[0].read_bytes() == content
    assert result['dataset_epoch'] != context['dataset_epoch']
    with pytest.raises(VoiceNoteError):
        service.transcribe(item['note_id'], result['dataset_epoch'])


def test_session_deletion_retains_session_after_failed_audio_removal_and_retries(voices, monkeypatch):
    v = voices
    item = upload(v)
    mid = v.store.save_message(v.sid, 'user', 'Keep this until attachment removal succeeds')
    with monkeypatch.context() as blocked:
        def fail(*args, **kwargs):
            raise PermissionError('fixture file in use')
        blocked.setattr('core.voice_notes.shutil.rmtree', fail)
        with pytest.raises(VoiceNoteError, match='could not be removed'):
            v.store.delete_session(v.sid)
    assert v.store.get_session(v.sid) is not None
    assert v.store.get_message(mid) is not None
    v.store.delete_session(v.sid)
    assert v.store.get_session(v.sid) is None and v.store.get_message(mid) is None
    assert not v.service._folder(item['note_id']).exists()
    with v.service.connection() as conn:
        assert conn.execute('SELECT count(*) FROM voice_notes WHERE session_id=?', (v.sid,)).fetchone()[0] == 0


def test_exclusive_session_cleanup_refuses_current_but_removes_prior_interrupted_note(voices):
    from core import state_gate, voice_notes
    v = voices
    item = upload(v)
    with v.service.connection() as conn:
        conn.execute("UPDATE voice_notes SET status='transcribing',process_id=? WHERE note_id=?",
                     (voice_notes.PROCESS_ID, item['note_id']))
    with state_gate.STATE.exclusive(), pytest.raises(VoiceNoteError):
        v.store.delete_session(v.sid, cleanup_only=True)
    assert v.store.get_session(v.sid) is not None and v.service._folder(item['note_id']).exists()
    with v.service.connection() as conn:
        conn.execute("UPDATE voice_notes SET process_id='prior-process' WHERE note_id=?", (item['note_id'],))
    with state_gate.STATE.exclusive():
        v.store.delete_session(v.sid, cleanup_only=True)
    assert v.store.get_session(v.sid) is None and not v.service._folder(item['note_id']).exists()


def test_deleting_linked_message_removes_recording_and_keeps_redacted_tombstone(voices):
    v = voices
    item = upload(v)
    mid = v.store.save_message(v.sid, 'user', 'Recorded message')
    later = v.store.save_message(v.sid, 'assistant', 'Later answer stays')
    with v.service.connection() as conn:
        conn.execute("UPDATE voice_notes SET user_message_id=?,status='sent' WHERE note_id=?", (mid, item['note_id']))
    v.store.change_message(mid, v.sid, delete=True)
    assert v.store.get_message(mid) is None and v.store.get_message(later).content == 'Later answer stays'
    assert not v.service._folder(item['note_id']).exists()
    row = v.service._raw(item['note_id'])
    assert row['error_stage'] == 'deleted' and row['transcript'] == '' and row['audio_bytes'] == 0
    assert v.service.list(v.sid) == []






def test_note_deletion_refuses_unexpected_nested_content_without_removing_it(voices):
    v = voices
    item = upload(v)
    folder = v.service._folder(item['note_id'])
    nested = folder / 'unexpected'
    nested.mkdir()
    (nested / 'keep.txt').write_text('synthetic', encoding='utf-8')
    with pytest.raises(VoiceNoteError, match='deletion path'):
        v.service.delete(item['note_id'])
    assert (nested / 'keep.txt').read_text('utf-8') == 'synthetic'
    assert v.service._raw(item['note_id'])['error_stage'] != 'deleted'


@pytest.mark.parametrize('kind', ['symlink', 'hardlink'])
def test_note_deletion_refuses_linked_files_and_preserves_original(voices, tmp_path, kind):
    v = voices
    item = upload(v)
    external = tmp_path / 'synthetic-external.txt'
    external.write_text('preserve', encoding='utf-8')
    link = v.service._folder(item['note_id']) / 'unexpected-link'
    try:
        if kind == 'symlink':
            link.symlink_to(external)
        else:
            os.link(external, link)
    except OSError:
        pytest.skip('This filesystem does not permit the synthetic link.')
    with pytest.raises(VoiceNoteError, match='deletion path'):
        v.service.delete(item['note_id'])
    assert external.read_text('utf-8') == 'preserve'
    assert v.service._raw(item['note_id'])['error_stage'] != 'deleted'
