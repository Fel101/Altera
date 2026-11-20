"""Durable local voice-note drafts and reviewed, idempotent chat attachments.

Audio is never transcribed or sent by an upload. SQLite is the job ledger; files
are published before a successful upload is acknowledged. Recovery never starts
model work. The chat route supplies the existing durable TurnService submission.
"""
from contextlib import contextmanager, nullcontext
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import stat
import threading
import time
import uuid

LOGGER = logging.getLogger(__name__)
PROCESS_ID = uuid.uuid4().hex
NOTE_ID = re.compile(r'vn_[0-9a-f]{32}\Z')
MAX_AUDIO_BYTES = 20 * 1024 * 1024
MAX_DURATION_SECONDS = 300
MAX_REPLY_BYTES = 64 * 1024 * 1024
MAX_TRANSCRIPT_CHARS = 10000
STATES = {'upload', 'transcribing', 'review', 'ready', 'sending', 'sent', 'failed', 'stale'}
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS voice_notes (
    note_id TEXT PRIMARY KEY,
    session_id INTEGER NOT NULL,
    avatar_id TEXT NOT NULL,
    dataset_epoch TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('upload','transcribing','review','ready','sending','sent','failed','stale')),
    upload_complete INTEGER NOT NULL DEFAULT 0,
    audio_sha256 TEXT NOT NULL DEFAULT '',
    audio_bytes INTEGER NOT NULL DEFAULT 0,
    audio_ext TEXT NOT NULL DEFAULT '',
    audio_mime TEXT NOT NULL DEFAULT '',
    duration_seconds REAL NOT NULL DEFAULT 0,
    transcript TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 0,
    reviewed_revision INTEGER NOT NULL DEFAULT -1,
    language TEXT NOT NULL DEFAULT '',
    request_id TEXT NOT NULL UNIQUE,
    user_message_id INTEGER,
    assistant_message_id INTEGER,
    reply_audio TEXT NOT NULL DEFAULT '[]',
    reply_audio_warning TEXT NOT NULL DEFAULT '',
    error_stage TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    process_id TEXT NOT NULL,
    source_revision INTEGER NOT NULL DEFAULT 0,
    recall_after INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_voice_notes_session ON voice_notes(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_voice_notes_owner ON voice_notes(avatar_id);
"""


class VoiceNoteError(ValueError):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


class RecordingIntegrityError(VoiceNoteError):
    def __init__(self):
        super().__init__('The saved recording is missing or damaged. Use a preserved browser original or record a new note.', 409)


def ensure_schema(conn):
    """Additive application-owned schema, also used by backup normalization."""
    conn.executescript(SCHEMA_SQL)


def _inspect_audio_source(source, header, size):
    """Validate a source against the voice-note media limits."""
    import av
    formats = {
        'wav': ({'pcm_s16le', 'pcm_s24le', 'pcm_s32le', 'pcm_u8', 'pcm_f32le'}, 'wav', 'audio/wav'),
        'matroska,webm': ({'opus', 'vorbis'}, 'webm', 'audio/webm'),
        'ogg': ({'opus', 'vorbis', 'flac'}, 'ogg', 'audio/ogg'),
        'mp3': ({'mp3', 'mp3float'}, 'mp3', 'audio/mpeg'),
        'mov,mp4,m4a,3gp,3g2,mj2': ({'aac'}, 'm4a', 'audio/mp4'),
    }
    # PyAV versions differ when a demuxer is forced: some report its short
    # name, while others report the full FFmpeg alias list.
    formats['matroska'] = formats['matroska,webm']
    formats['mov'] = formats['mov,mp4,m4a,3gp,3g2,mj2']
    try:
        # Force a recognized demuxer before libav can interpret a playlist or URL.
        if header[:4] == b'RIFF' and header[8:12] == b'WAVE':
            demuxer = 'wav'
            if int.from_bytes(header[4:8], 'little') + 8 > size:
                raise VoiceNoteError('The WAV recording is incomplete.', 422)
        elif header.startswith(b'\x1a\x45\xdf\xa3'):
            demuxer = 'matroska'
        elif header.startswith(b'OggS'):
            demuxer = 'ogg'
        elif header.startswith(b'ID3') or (len(header) > 1 and header[0] == 255 and header[1] & 224 == 224):
            demuxer = 'mp3'
        elif header[4:8] == b'ftyp':
            demuxer = 'mov'
        else:
            raise VoiceNoteError('The recording is not a recognized audio container.', 422)
        started, duration, count = time.monotonic(), 0., 0
        opened = av.open(source, mode='r', format=demuxer,
                         options={'protocol_whitelist': 'file', 'err_detect': 'explode'})
        with opened as container:
            allowed = formats.get(container.format.name)
            if not allowed or len(container.streams) != 1 or container.streams[0].type != 'audio':
                raise VoiceNoteError('Use an audio-only WAV, WebM, Ogg, MP3 or M4A recording.', 422)
            stream = container.streams[0]
            if stream.codec_context.name not in allowed[0]:
                raise VoiceNoteError('The recording uses an unsupported audio codec.', 422)
            declared = float(container.duration / av.time_base) if container.duration is not None else None
            frames = container.decode(stream)
            frame_owner = nullcontext(frames)
            with frame_owner:
                for frame in frames:
                    count += 1
                    rate = frame.sample_rate
                    if not rate or rate < 0 or rate > 96000 or len(frame.layout.channels) > 2:
                        raise VoiceNoteError('The recording must have at most two channels and a sample rate no higher than 96 kHz.', 422)
                    duration += frame.samples / rate
                    if count > 100000 or time.monotonic() - started > 20:
                        raise VoiceNoteError('The recording could not be checked within the audio limits.', 422)
                    if duration > MAX_DURATION_SECONDS:
                        raise VoiceNoteError('Voice notes can be at most five minutes long.', 422)
            if not math.isfinite(duration) or duration <= 0:
                raise VoiceNoteError('The recording contains no readable audio.', 422)
            if demuxer != 'mp3' and declared and declared - duration > max(1., declared * .05):
                raise VoiceNoteError('The recording is incomplete; its duration does not match the readable audio.', 422)
            return {'duration_seconds': duration, 'ext': allowed[1], 'mime': allowed[2]}
    except VoiceNoteError:
        raise
    except Exception as exc:
        raise VoiceNoteError('The recording is damaged or is not a supported audio file.', 422) from exc


def inspect_audio(path):
    """Check the actual container/codec and bounded decoded duration, not MIME."""
    try:
        with Path(path).open('rb') as source:
            header = source.read(32)
        return _inspect_audio_source(str(path), header, Path(path).stat().st_size)
    except VoiceNoteError:
        raise
    except Exception as exc:
        raise VoiceNoteError('The recording is damaged or is not a supported audio file.', 422) from exc


def _verified_file_size(path, expected_hash, maximum):
    """Hash a bounded regular file without buffering it."""
    if (not isinstance(expected_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', expected_hash)
            or path.is_symlink() or not path.is_file()):
        return None
    size, digest = 0, hashlib.sha256()
    try:
        with path.open('rb') as source:
            while block := source.read(65536):
                size += len(block)
                if size > maximum:
                    return None
                digest.update(block)
    except OSError:
        return None
    return size if size and digest.hexdigest() == expected_hash else None


def _verified_reply_size(path, filename):
    """Verify a retained clip against its content-addressed name."""
    match = re.fullmatch(r'reply-([0-9a-f]{64})\.(?:wav|mp3)', filename)
    return _verified_file_size(path, match[1], MAX_REPLY_BYTES) if match else None


def recording_context(store, session_id):
    """Read a review boundary without starting voice recovery or background jobs."""
    from core.knowledge import floor
    if type(session_id) is not int or session_id <= 0:
        raise VoiceNoteError('session_id must be a positive integer.', 400)
    session = store.get_session(session_id)
    if not session:
        raise VoiceNoteError('Conversation not found.', 404)
    return {'session_id': session_id, 'avatar_id': session.avatar_id or '',
            'dataset_epoch': store.dataset_epoch()['value'],
            'source_revision': int((store.get_key(f'knowledge_dirty:{session_id}') or {}).get('revision') or 0),
            'recall_after': floor(store, session.avatar_id or '')}


class VoiceNoteService:
    def __init__(self, store, directory=None, *, transcriber=None, parser=None, sync=False, recover=True):
        from core.paths import data_path
        self.store = store
        folder = Path(directory or data_path('voice_notes'))
        try:
            info = folder.lstat()
            if (stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400
                    or not stat.S_ISDIR(info.st_mode)):
                raise VoiceNoteError('The voice-note storage path is unavailable.', 409)
        except FileNotFoundError:
            pass
        self.root = folder.resolve()
        self.transcriber, self.parser, self.sync = transcriber, parser or inspect_audio, sync
        self.lock = threading.RLock()
        self.jobs = set()
        with self.connection() as conn:
            ensure_schema(conn)
        from core.turns import get_turn_service
        get_turn_service(store)  # Ledger recovery marks previous-process replies interrupted; never resubmits.
        if recover:
            self.reconcile()

    @contextmanager
    def connection(self):
        conn = self.store._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _id(self, note_id):
        if not isinstance(note_id, str) or not NOTE_ID.fullmatch(note_id):
            raise VoiceNoteError('Invalid voice-note ID.', 400)
        return note_id

    def _folder(self, note_id):
        target = self.root / self._id(note_id)
        try:
            root_info = self.root.lstat()
            if (stat.S_ISLNK(root_info.st_mode) or getattr(root_info, 'st_file_attributes', 0) & 0x400
                    or not stat.S_ISDIR(root_info.st_mode)):
                raise VoiceNoteError('The voice-note storage path is unavailable.', 409)
        except FileNotFoundError:
            pass
        try:
            target.resolve().relative_to(self.root)
        except ValueError:
            raise VoiceNoteError('The voice-note storage path is unavailable.', 409) from None
        if self.root.is_symlink() or target.is_symlink():
            raise VoiceNoteError('The voice-note storage path is unavailable.', 409)
        return target

    def _path(self, row):
        if row['audio_ext'] not in {'wav', 'webm', 'ogg', 'mp3', 'm4a'}:
            raise VoiceNoteError('The recording is not available.', 404)
        path = self._folder(row['note_id']) / ('input.' + row['audio_ext'])
        if path.is_symlink() or not path.is_file():
            raise VoiceNoteError('The recording is not available.', 404)
        if (type(row['audio_bytes']) is not int or not 0 < row['audio_bytes'] <= MAX_AUDIO_BYTES or
                _verified_file_size(path, row['audio_sha256'], MAX_AUDIO_BYTES) != row['audio_bytes']):
            raise RecordingIntegrityError()
        return path

    def _raw(self, note_id):
        with self.connection() as conn:
            row = conn.execute('SELECT * FROM voice_notes WHERE note_id=?', (self._id(note_id),)).fetchone()
            return dict(row) if row else None

    def _update(self, note_id, **changes):
        # Field names are internal constants at each call site.
        with self.connection() as conn:
            conn.execute('UPDATE voice_notes SET ' + ','.join(k + '=?' for k in changes) +
                         ',updated_at=CURRENT_TIMESTAMP WHERE note_id=?', (*changes.values(), note_id))

    def _epoch(self, epoch):
        current = self.store.get_key('dataset_epoch') or {}
        if not isinstance(epoch, str) or not epoch or epoch != current.get('value'):
            raise VoiceNoteError('Altera’s data changed. Reload and record a new voice note before sending.', 409)
        return epoch

    def _session(self, session_id):
        if type(session_id) is not int or session_id <= 0:
            raise VoiceNoteError('session_id must be a positive integer.', 400)
        session = self.store.get_session(session_id)
        if not session:
            raise VoiceNoteError('Conversation not found.', 404)
        return session

    def _owned(self, note_id, session_id=None):
        row = self._raw(note_id)
        if not row or row['error_stage'] == 'deleted' or (session_id is not None and row['session_id'] != session_id):
            raise VoiceNoteError('Voice note not found for this conversation.', 404)
        session = self._session(row['session_id'])
        if (session.avatar_id or '') != row['avatar_id']:
            raise VoiceNoteError('Voice note not found for this companion.', 404)
        return row

    def _current(self, row, epoch):
        from core.knowledge import floor
        self._epoch(epoch)
        revision = (self.store.get_key(f"knowledge_dirty:{row['session_id']}") or {}).get('revision', 0)
        if (row['dataset_epoch'] != epoch or row['status'] == 'stale' or
                floor(self.store, row['avatar_id']) != row['recall_after'] or revision != row['source_revision']):
            self._update(row['note_id'], status='stale', error_stage='context', error='Saved context changed. Record a new voice note.')
            raise VoiceNoteError('Saved context changed. Record a new voice note before sending.', 409)

    def _public(self, row):
        replies = self._replies(row)
        available = self._replies(row, verified=True)
        warning = row['reply_audio_warning']
        if len(available) != len(replies) and not warning:
            warning = 'Some reply audio could not be retained; saved reply text is still available.'
        audio_url, audio_warning = None, ''
        if row['upload_complete']:
            try:
                self._path(row)
            except VoiceNoteError:
                audio_warning = 'The saved recording is missing or damaged. Use a preserved browser original or record a new note.'
            else:
                audio_url = f"/api/voice-notes/{row['note_id']}/audio"
        return {**{key: row[key] for key in ('note_id', 'session_id', 'avatar_id', 'dataset_epoch', 'status', 'upload_complete',
            'audio_sha256', 'audio_bytes', 'duration_seconds', 'transcript', 'revision', 'reviewed_revision', 'language',
            'request_id', 'user_message_id', 'assistant_message_id', 'error_stage', 'error',
            'source_revision', 'recall_after', 'created_at', 'updated_at')},
            'audio_url': audio_url, 'audio_warning': audio_warning,
            'reply_audio': available, 'reply_audio_warning': warning}

    def _replies(self, row, *, verified=False):
        try:
            records = json.loads(row['reply_audio'])
            if not isinstance(records, list) or len(records) > 128:
                return []
            replies, total = [], 0
            for item in records:
                if (not isinstance(item, dict) or not isinstance(item.get('filename'), str) or
                        not re.fullmatch(r'reply-[0-9a-f]{64}\.(wav|mp3)', item['filename']) or
                        not isinstance(item.get('original_url'), str) or
                        not re.fullmatch(r'/static/audio/tts/[a-zA-Z0-9_-]{1,160}\.(wav|mp3)', item['original_url'])):
                    continue
                if verified:
                    size = _verified_reply_size(self._folder(row['note_id']) / item['filename'], item['filename'])
                    if size is None or total + size > MAX_REPLY_BYTES:
                        continue
                    total += size
                replies.append({'filename': item['filename'], 'original_url': item['original_url'],
                                'audio_url': f"/api/voice-notes/{row['note_id']}/replies/{item['filename']}"})
            return replies
        except (ValueError, TypeError):
            return []

    def get(self, note_id, session_id=None):
        with self.lock:
            row = self._owned(note_id, session_id)
            self._reconcile_row(row)
            return self._public(self._owned(note_id, session_id))

    def list(self, session_id):
        session = self._session(session_id)
        with self.lock, self.connection() as conn:
            ids = [r[0] for r in conn.execute("SELECT note_id FROM voice_notes WHERE session_id=? AND avatar_id=? AND error_stage!='deleted' ORDER BY created_at,note_id",
                                            (session_id, session.avatar_id or ''))]
        return [self.get(note_id, session_id) for note_id in ids]

    def context(self, session_id):
        return recording_context(self.store, session_id)

    def upload(self, note_id, session_id, epoch, stream, *, source_revision=None, recall_after=None, avatar_id=None):
        self._id(note_id)
        with self.lock:
            self._epoch(epoch)
            session = self._session(session_id)
            owner = session.avatar_id or ''
            current = self.context(session_id)
            if (type(source_revision) is not int or source_revision < 0 or type(recall_after) is not int or recall_after < 0
                    or not isinstance(avatar_id, str)):
                raise VoiceNoteError('Capture the current recording context before uploading audio.', 400)
            if (source_revision != current['source_revision'] or recall_after != current['recall_after'] or
                    avatar_id != owner):
                raise VoiceNoteError('The recording context changed. Record a new voice note before uploading.', 409)
            old = self._raw(note_id)
            if old and old['status'] == 'stale':
                raise VoiceNoteError('This voice note was deleted or its context changed. Record a new note with a new ID.', 410)
            if old and (old['session_id'] != session_id or old['avatar_id'] != owner or old['dataset_epoch'] != epoch):
                raise VoiceNoteError('This voice-note ID belongs to a different recording context.', 409)
            if old:
                self._current(old, epoch)
            folder = self._folder(note_id)
            if (self.root / ('.delete-' + note_id)).exists():
                raise VoiceNoteError('Finish deleting this voice note before uploading again.')
            folder.mkdir(parents=True, exist_ok=True)
            temp = folder / ('.upload-' + uuid.uuid4().hex)
            published = None
            if not old:
                with self.connection() as conn:
                    conn.execute('INSERT INTO voice_notes(note_id,session_id,avatar_id,dataset_epoch,status,request_id,process_id,source_revision,recall_after) '
                                 "VALUES(?,?,?,?,'upload',?,?,?,?)", (note_id, session_id, owner, epoch, 'voice_' + note_id, PROCESS_ID, source_revision, recall_after))
            try:
                digest, size = hashlib.sha256(), 0
                with temp.open('xb') as output:
                    while True:
                        block = stream.read(65536)
                        if not block:
                            break
                        size += len(block)
                        if size > MAX_AUDIO_BYTES:
                            raise VoiceNoteError('Voice notes must be no larger than 20 MiB.', 413)
                        digest.update(block)
                        output.write(block)
                    output.flush()
                    os.fsync(output.fileno())
                if not size:
                    raise VoiceNoteError('The recording is empty.', 400)
                sha = digest.hexdigest()
                if old and old['upload_complete']:
                    if sha != old['audio_sha256'] or size != old['audio_bytes']:
                        raise VoiceNoteError('This voice-note ID already belongs to different audio.', 409)
                    self._path(old)
                    self._current(self._owned(note_id), epoch)
                    return self._public(self._raw(note_id))
                verified = self.parser(temp)
                self._current(self._owned(note_id), epoch)
                published = folder / ('input.' + verified['ext'])
                with self.connection() as conn:
                    conn.execute('BEGIN IMMEDIATE')
                    latest = conn.execute('SELECT avatar_id FROM sessions WHERE id=?', (session_id,)).fetchone()
                    keys = ('dataset_epoch', f'knowledge_dirty:{session_id}', 'knowledge_recall:' + (owner or '@default'))
                    states = {r['key']: json.loads(r['value']) for r in conn.execute('SELECT key,value FROM user_profile WHERE key IN (?,?,?)', keys)}
                    if (not latest or (latest['avatar_id'] or '') != owner or states.get('dataset_epoch', {}).get('value') != epoch or
                            int(states.get(keys[1], {}).get('revision') or 0) != source_revision or
                            int(states.get(keys[2], {}).get('through') or 0) != recall_after or
                            conn.execute('SELECT status FROM voice_notes WHERE note_id=?', (note_id,)).fetchone()[0] == 'stale'):
                        raise VoiceNoteError('The recording context changed while uploading. Record a new voice note.')
                    os.replace(temp, published)
                    conn.execute("UPDATE voice_notes SET status='upload',upload_complete=1,audio_sha256=?,audio_bytes=?,audio_ext=?,audio_mime=?,"
                                 "duration_seconds=?,error='',error_stage='',process_id=?,updated_at=CURRENT_TIMESTAMP WHERE note_id=?",
                                 (sha, size, verified['ext'], verified['mime'], verified['duration_seconds'], PROCESS_ID, note_id))
                return self._public(self._raw(note_id))
            except Exception:
                if not old or not old['upload_complete']:
                    if published:
                        published.unlink(missing_ok=True)
                    current_row = self._raw(note_id)
                    if current_row and current_row['status'] != 'stale':
                        self._update(note_id, status='failed', error_stage='upload', error='The recording was not saved. Retry the upload explicitly.')
                raise
            finally:
                temp.unlink(missing_ok=True)

    def transcribe(self, note_id, epoch):
        with self.lock:
            row = self._owned(note_id)
            self._current(row, epoch)
            if row['status'] == 'transcribing':
                return self._public(row)
            if row['status'] not in {'upload', 'failed'} or row['error_stage'] == 'send' or not row['upload_complete']:
                raise VoiceNoteError('This recording is not waiting for transcription.')
            self._path(row)
            self._update(note_id, status='transcribing', process_id=PROCESS_ID, error='', error_stage='')
            self.jobs.add(note_id)
            if self.sync:
                self._transcribe(note_id)
                row = self._raw(note_id)
                if row['status'] == 'failed':
                    raise VoiceNoteError(row['error'], 503)
            else:
                from core.state_gate import start_job
                try:
                    start_job(self._transcribe, (note_id,), name='altera-voice-note')
                except Exception:
                    self.jobs.discard(note_id)
                    self._update(note_id, status='failed', error_stage='transcribe', error='The local transcription worker could not start.')
                    raise
            return self._public(self._raw(note_id))

    def _transcribe(self, note_id):
        try:
            with self.lock:
                row = self._owned(note_id)
                self._current(row, row['dataset_epoch'])
                if row['status'] != 'transcribing' or row['process_id'] != PROCESS_ID:
                    return
            transcriber = self.transcriber
            options = {'offline_only': True}
            if transcriber is None:
                from core.config import get_config
                from core.stt import transcribe_from_bytes
                transcriber = transcribe_from_bytes
                options['model_size'] = get_config().get('voice', {}).get('stt_model', 'base')
            try:
                with self._path(row).open('rb') as source:
                    audio = source.read(MAX_AUDIO_BYTES + 1)
            except OSError:
                raise RecordingIntegrityError() from None
            if len(audio) != row['audio_bytes'] or hashlib.sha256(audio).hexdigest() != row['audio_sha256']:
                raise RecordingIntegrityError()
            result = transcriber(audio, **options)
            text = result.get('text')
            if result.get('error') or not isinstance(text, str) or not text.strip() or len(text) > MAX_TRANSCRIPT_CHARS:
                raise VoiceNoteError('Local transcription is unavailable or found no speech. Check cached Whisper weights, then retry explicitly.', 503)
            with self.lock:
                current = self._owned(note_id)
                self._current(current, row['dataset_epoch'])
                if current['status'] != 'transcribing' or current['process_id'] != PROCESS_ID:
                    return
                self._update(note_id, status='review', transcript=text.strip(), revision=current['revision'] + 1,
                             reviewed_revision=-1, language=str(result.get('language') or '')[:50], error='', error_stage='')
        except Exception as exc:
            LOGGER.warning('Local voice-note transcription did not complete: %s', note_id)
            with self.lock:
                row = self._raw(note_id)
                if row and row['status'] != 'stale':
                    message = (str(RecordingIntegrityError()) if isinstance(exc, RecordingIntegrityError) else
                               'Local transcription is unavailable or found no speech. Check cached Whisper weights, then retry explicitly.')
                    self._update(note_id, status='failed', error_stage='transcribe', error=message)
        finally:
            with self.lock:
                self.jobs.discard(note_id)

    def edit(self, note_id, epoch, transcript, revision):
        if not isinstance(transcript, str) or not transcript.strip() or len(transcript) > MAX_TRANSCRIPT_CHARS:
            raise VoiceNoteError('Transcript must be nonempty text of at most 10,000 characters.', 400)
        with self.lock:
            row = self._owned(note_id)
            self._current(row, epoch)
            if row['status'] not in {'review', 'ready'} or type(revision) is not int or revision != row['revision']:
                raise VoiceNoteError('Reload the current transcript before editing it.')
            self._update(note_id, transcript=transcript.strip(), revision=revision + 1, reviewed_revision=-1, status='review')
            return self._public(self._raw(note_id))

    def review(self, note_id, epoch, revision):
        with self.lock:
            row = self._owned(note_id)
            self._current(row, epoch)
            if row['status'] not in {'review', 'ready'} or type(revision) is not int or revision != row['revision'] or not row['transcript'].strip():
                raise VoiceNoteError('Reload and review the current transcript before sending.')
            self._update(note_id, status='ready', reviewed_revision=revision)
            return self._public(self._raw(note_id))

    def prepare_send(self, note_id, session_id, epoch, reviewed_revision):
        from core import safety
        if type(session_id) is not int or session_id <= 0:
            raise VoiceNoteError('session_id must be a positive integer.', 400)
        with self.lock:
            row = self._owned(note_id, session_id)
            self._current(row, epoch)
            replay_failed = row['status'] == 'failed' and row['error_stage'] == 'send'
            if (type(reviewed_revision) is not int or row['reviewed_revision'] != reviewed_revision or
                    row['revision'] != reviewed_revision or (row['status'] not in {'ready', 'sending', 'sent'} and not replay_failed)):
                raise VoiceNoteError('Review the current transcript before sending it.')
            if safety.blocks_message(row['transcript']):
                raise VoiceNoteError('That transcript cannot be sent.', 422)
            if row['status'] == 'ready':
                self._update(note_id, status='sending', process_id=PROCESS_ID, error='', error_stage='')
            return {'voice_note_id': note_id, 'request_id': row['request_id'], 'session_id': row['session_id'],
                    'message': row['transcript'], 'reviewed_revision': reviewed_revision}

    def mark_submitted(self, note_id, turnrow):
        with self.lock:
            row = self._owned(note_id)
            if turnrow['request_id'] != row['request_id'] or turnrow['session_id'] != row['session_id']:
                raise VoiceNoteError('The reply does not belong to this voice note.')
            self._reconcile_row(row)
            return self._public(self._raw(note_id))

    def abort_send(self, note_id):
        with self.lock:
            row = self._owned(note_id)
            with self.connection() as conn:
                exists = conn.execute('SELECT 1 FROM chat_turns WHERE request_id=?', (row['request_id'],)).fetchone()
                retired = conn.execute('SELECT 1 FROM chat_request_tombstones WHERE request_id=?', (row['request_id'],)).fetchone()
            if exists or retired:
                self._reconcile_row(row)
            elif row['status'] == 'sending':
                self._update(note_id, status='ready', error='', error_stage='')
            return self._public(self._raw(note_id))

    def _retain_reply_audio(self, row):
        from core import tts
        cache = Path(tts.AUDIO_CACHE_DIR).resolve()
        retained, total, missing = [], 0, 0
        previous = {item['original_url']: item for item in self._replies(row)}
        with self.connection() as conn:
            events = conn.execute('SELECT event FROM chat_turn_events WHERE request_id=? ORDER BY id', (row['request_id'],)).fetchall()
        for event in events:
            try:
                record = json.loads(event[0])
                url = record.get('audio_url') if isinstance(record, dict) else None
            except (ValueError, TypeError):
                continue
            if url is not None and not isinstance(url, str):
                missing += 1
                continue
            if not url or url in {item['original_url'] for item in retained}:
                continue
            previous_item = previous.get(url)
            if url in previous:
                old_file = self._folder(row['note_id']) / previous[url]['filename']
                if old_file.exists() or old_file.is_symlink():
                    size = _verified_reply_size(old_file, previous[url]['filename'])
                    if size is not None and total + size <= MAX_REPLY_BYTES:
                        total += size
                        retained.append(previous[url])
                    else:
                        missing += 1
                        retained.append(previous[url])  # Keep the expected hash for later review.
                    continue
            match = re.fullmatch(r'/static/audio/tts/([a-zA-Z0-9_-]{1,160}\.(?:wav|mp3))', url)
            temp = None
            try:
                source = cache / match[1] if match else None
                if source is None or source.is_symlink() or not source.is_file() or len(retained) >= 128:
                    raise OSError('audio unavailable')
                size = source.stat().st_size
                total += size
                if size <= 0 or size > MAX_REPLY_BYTES or total > MAX_REPLY_BYTES:
                    raise OSError('audio too large')
                with source.open('rb') as cached:
                    content = cached.read(size + 1)
                if len(content) != size:
                    raise OSError('audio changed during read')
                filename = 'reply-' + hashlib.sha256(content).hexdigest() + source.suffix
                if previous_item and previous_item['filename'] != filename:
                    raise OSError('the source no longer matches the retained audio')
                target = self._folder(row['note_id']) / filename
                if target.exists() or target.is_symlink():
                    if _verified_reply_size(target, filename) is None:
                        previous_item = previous_item or {'filename': filename, 'original_url': url}
                        raise OSError('existing retained audio is damaged')
                    retained.append({'filename': filename, 'original_url': url, 'audio_url': f"/api/voice-notes/{row['note_id']}/replies/{filename}"})
                    continue
                temp = target.parent / ('.reply-' + uuid.uuid4().hex)
                with temp.open('xb') as output:
                    output.write(content)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temp, target)
                retained.append({'filename': filename, 'original_url': url, 'audio_url': f"/api/voice-notes/{row['note_id']}/replies/{filename}"})
            except OSError:
                missing += 1
                if previous_item:
                    retained.append(previous_item)
            finally:
                if temp is not None:
                    try:
                        temp.unlink(missing_ok=True)
                    except OSError:
                        LOGGER.warning('A voice-note audio staging file awaits cleanup: %s', row['note_id'])
        return retained, 'Some reply audio could not be retained; saved reply text is still available.' if missing else ''

    def _reconcile_row(self, row):
        if row['status'] == 'stale':
            return
        epoch = (self.store.get_key('dataset_epoch') or {}).get('value')
        try:
            self._current(row, epoch)
        except VoiceNoteError:
            return
        if row['process_id'] != PROCESS_ID and (row['status'] == 'transcribing' or (row['status'] == 'upload' and not row['upload_complete'])):
            self._update(row['note_id'], status='failed', error_stage='transcribe' if row['status'] == 'transcribing' else 'upload',
                         error='Altera restarted before this work finished. Retry explicitly.')
            return
        with self.connection() as conn:
            turn = conn.execute('SELECT * FROM chat_turns WHERE request_id=?', (row['request_id'],)).fetchone()
            retired = conn.execute('SELECT 1 FROM chat_request_tombstones WHERE request_id=?', (row['request_id'],)).fetchone()
        if retired:
            self._update(row['note_id'], status='stale', error_stage='context', error='The attached conversation changed. Record a new voice note.')
        elif turn:
            status = 'sending' if turn['status'] in {'sending', 'streaming'} else ('sent' if turn['assistant_message_id'] else 'failed')
            retained, warning = self._retain_reply_audio(row) if status == 'sent' else (self._replies(row), row['reply_audio_warning'])
            self._update(row['note_id'], status=status, user_message_id=turn['user_message_id'], assistant_message_id=turn['assistant_message_id'],
                         reply_audio=json.dumps(retained), reply_audio_warning=warning,
                         error_stage='send' if status == 'failed' else '', error=turn['error'] or '' if status == 'failed' else '')
        elif row['status'] == 'sending' and row['process_id'] != PROCESS_ID:
            self._update(row['note_id'], status='ready', error_stage='', error='No reply was submitted before restart. Sending again requires your action.')

    def reconcile(self, note_id=None):
        with self.lock, self.connection() as conn:
            rows = conn.execute('SELECT * FROM voice_notes' + (' WHERE note_id=?' if note_id else ''), (note_id,) if note_id else ()).fetchall()
            for row in rows:
                self._reconcile_row(dict(row))

    def settle_turn(self, request_id):
        """Complete attachment retention under the reply worker's existing lease."""
        with self.lock, self.connection() as conn:
            rows = conn.execute("SELECT * FROM voice_notes WHERE request_id=? AND error_stage!='deleted'", (request_id,)).fetchall()
            for row in rows:
                self._reconcile_row(dict(row))

    def audio(self, note_id, filename=None):
        with self.lock:
            row = self._owned(note_id)
            if filename is None:
                return self._path(row), row['audio_mime']
            if not re.fullmatch(r'reply-[0-9a-f]{64}\.(wav|mp3)', filename) or filename not in {a['filename'] for a in self._replies(row)}:
                raise VoiceNoteError('Reply audio not found.', 404)
            path = self._folder(note_id) / filename
            if path.is_symlink() or not path.is_file():
                raise VoiceNoteError('Reply audio not found.', 404)
            if _verified_reply_size(path, filename) is None:
                raise VoiceNoteError('The saved reply audio is damaged or unavailable.', 409)
            return path, 'audio/wav' if path.suffix == '.wav' else 'audio/mpeg'

    def invalidate_owner(self, owner, reason='Saved context changed. Record a new voice note.'):
        with self.lock, self.connection() as conn:
            conn.execute("UPDATE voice_notes SET status='stale',error_stage='context',error=?,updated_at=CURRENT_TIMESTAMP WHERE avatar_id=? AND error_stage!='deleted'", (reason, owner or ''))

    def invalidate_session(self, session_id, reason='The conversation changed. Record a new voice note.'):
        with self.lock, self.connection() as conn:
            conn.execute("UPDATE voice_notes SET status='stale',error_stage='context',error=?,updated_at=CURRENT_TIMESTAMP WHERE session_id=? AND error_stage!='deleted'", (reason, session_id))

    def delete(self, note_id, *, allow_interrupted=False):
        with self.lock:
            row = self._raw(note_id)
            if not row:
                raise VoiceNoteError('Voice note not found.', 404)
            with self.connection() as conn:
                turn = conn.execute('SELECT status FROM chat_turns WHERE request_id=?', (row['request_id'],)).fetchone()
            active = turn and turn['status'] in {'sending', 'streaming'}
            interrupted = allow_interrupted and row['process_id'] != PROCESS_ID
            reserved = row['status'] == 'sending' and not turn and not interrupted
            if note_id in self.jobs or (row['status'] == 'transcribing' and not interrupted) or reserved or active:
                raise VoiceNoteError('Wait for transcription or the reply to finish before deleting this voice note.')
            folder = self._folder(note_id)
            trash = self.root / ('.delete-' + self._id(note_id))
            if trash.is_symlink():
                raise VoiceNoteError('The voice-note deletion path is unavailable.')
            try:
                trash.resolve().relative_to(self.root)
            except ValueError:
                raise VoiceNoteError('The voice-note deletion path is unavailable.') from None
            try:
                self._check_delete_tree(folder)
                self._check_delete_tree(trash)
                if folder.exists() and not trash.exists():
                    os.replace(folder, trash)
                if folder.exists():
                    shutil.rmtree(folder)
                if trash.exists():
                    shutil.rmtree(trash)
            except OSError as exc:
                self._update(note_id, status='stale', error_stage='delete',
                             error='Deletion is incomplete. Stop playback and retry deleting this voice note.')
                raise VoiceNoteError('The saved audio could not be removed. Some linked audio may already be removed; stop playback and retry.') from exc
            # Keep a redacted tombstone: delayed offline upload retries cannot resurrect the note.
            self._update(note_id, status='stale', error_stage='deleted', error='This voice note was deleted.',
                         upload_complete=0, audio_sha256='', audio_bytes=0, audio_ext='', audio_mime='', duration_seconds=0,
                         transcript='', revision=0, reviewed_revision=-1, language='', user_message_id=None,
                         assistant_message_id=None, reply_audio='[]', reply_audio_warning='')

    @staticmethod
    def _check_delete_tree(folder):
        """Refuse links, reparse points, aliases and unexpected nested content."""
        def linked(info):
            return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)
        try:
            info = folder.lstat()
        except FileNotFoundError:
            return
        if linked(info) or not stat.S_ISDIR(info.st_mode):
            raise VoiceNoteError('The voice-note deletion path is unavailable.')
        count = 0
        for item in folder.iterdir():
            count += 1
            if count > 1024:
                raise VoiceNoteError('The voice-note deletion path is unavailable.')
            info = item.lstat()
            if linked(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise VoiceNoteError('The voice-note deletion path is unavailable.')

    def _clear(self, clause='', args=(), *, allow_interrupted=False):
        with self.lock, self.connection() as conn:
            ids = [r[0] for r in conn.execute('SELECT note_id FROM voice_notes' + clause, args)]
        for note_id in ids:
            self.delete(note_id, allow_interrupted=allow_interrupted)

    def clear_owner(self, owner):
        self._clear(' WHERE avatar_id=?', (owner or '',))

    def clear_session(self, session_id, *, allow_interrupted=False):
        self._clear(' WHERE session_id=?', (session_id,), allow_interrupted=allow_interrupted)

    def clear_message(self, message_id, *, allow_interrupted=False):
        if type(message_id) is not int or message_id <= 0:
            raise VoiceNoteError('message_id must be a positive integer.', 400)
        # The ledger can finish before the browser polls the note's cached links.
        self._clear(' WHERE user_message_id=? OR assistant_message_id=? OR request_id IN '
                    '(SELECT request_id FROM chat_turns WHERE user_message_id=? OR assistant_message_id=?)',
                    (message_id, message_id, message_id, message_id), allow_interrupted=allow_interrupted)

    def clear_all(self):
        self._clear()


_service_lock = threading.Lock()


def get_voice_note_service(store=None):
    from core.datastore import get_datastore
    store = store or get_datastore()
    with _service_lock:
        if not hasattr(store, '_voice_note_service'):
            store._voice_note_service = VoiceNoteService(store)
        return store._voice_note_service


def settle_linked_turn(store, request_id):
    """Retain voice reply media only when this turn belongs to a voice note.

    Ordinary text replies should not initialize voice-note recovery or rewrite
    unrelated recordings. The datastore already owns the voice-note schema.
    """
    conn = store._connect()
    try:
        linked = conn.execute("SELECT 1 FROM voice_notes WHERE request_id=? AND error_stage!='deleted' LIMIT 1",
                              (request_id,)).fetchone()
    finally:
        conn.close()
    if linked:
        get_voice_note_service(store).settle_turn(request_id)


def get_voice_note_cleanup_service(store):
    """Use an existing service or an unregistered cleanup-only instance.

    Used for cleanup under exclusive admission, without reconciling unrelated notes.
    """
    with _service_lock:
        existing = getattr(store, '_voice_note_service', None)
        return existing if existing is not None else VoiceNoteService(store, recover=False)
