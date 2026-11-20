"""Opted-in companion presence: bounded jobs, a private inbox, explicit acceptance.

An idle scheduler owns no state lease. Only a claimed generation job can call the
configured model, with one attempt and a short SDK timeout. Reading the inbox or
accepting its original text never generates, speaks, or starts photo work.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, UTC
import hashlib
import ipaddress
import json
import logging
import re
import threading
import time
import uuid
from urllib.parse import urlsplit

from core import presence_schedule as schedule

LOGGER = logging.getLogger(__name__)
UTC = UTC
PROCESS_ID = uuid.uuid4().hex
TIMEOUT_SECONDS = 15
MAX_TOKENS = 256
_generation_lock = threading.Lock()
DEFAULTS = {'enabled': False, 'timezone': 'UTC', 'quiet_enabled': True, 'quiet_start': 1320, 'quiet_end': 480,
            'interval_minutes': 360, 'cooldown_minutes': 360, 'daily_cap': 2, 'expiry_minutes': 120, 'provider_approval': None}
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS presence_preferences (
 avatar_id TEXT PRIMARY KEY, session_id INTEGER NOT NULL, dataset_epoch TEXT NOT NULL,
 revision INTEGER NOT NULL, enabled INTEGER NOT NULL DEFAULT 0, settings TEXT NOT NULL,
 needs_review INTEGER NOT NULL DEFAULT 0, next_due_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS presence_rituals (
 id TEXT PRIMARY KEY, avatar_id TEXT NOT NULL, session_id INTEGER NOT NULL, dataset_epoch TEXT NOT NULL,
 revision INTEGER NOT NULL, kind TEXT NOT NULL, minute INTEGER NOT NULL, weekday INTEGER,
 enabled INTEGER NOT NULL, deleted INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS presence_jobs (
 id TEXT PRIMARY KEY, avatar_id TEXT NOT NULL, session_id INTEGER NOT NULL, dataset_epoch TEXT NOT NULL,
 kind TEXT NOT NULL, ritual_id TEXT, occurrence TEXT NOT NULL, due_at REAL NOT NULL, expires_at REAL NOT NULL,
 state TEXT NOT NULL, process_id TEXT NOT NULL DEFAULT '', claim_token TEXT NOT NULL DEFAULT '',
 attempts INTEGER NOT NULL DEFAULT 0, snapshot TEXT NOT NULL, error TEXT NOT NULL DEFAULT '',
 created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_presence_jobs_due ON presence_jobs(state,due_at);
CREATE TABLE IF NOT EXISTS presence_inbox (
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL UNIQUE, avatar_id TEXT NOT NULL, session_id INTEGER NOT NULL,
 dataset_epoch TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL, created_at REAL NOT NULL,
 expires_at REAL NOT NULL, snapshot TEXT NOT NULL, invalidated INTEGER NOT NULL DEFAULT 0,
 error TEXT NOT NULL DEFAULT '', read_at REAL, dismissed_at REAL, accepted_at REAL,
 accepted_message_id INTEGER UNIQUE);
CREATE INDEX IF NOT EXISTS idx_presence_inbox_owner ON presence_inbox(avatar_id,session_id,created_at);
"""


class PresenceError(ValueError):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


def _pal_allows(owner, feature, error):
    """A pal only takes part when this feature is switched on in its settings (companions always may)."""
    from core import companion, companions
    if not companions.feature_on(owner, feature):
        record = companion.get_services().avatars.get(owner) if owner else None
        if feature == "presence" and (record or {}).get("kids") is True:
            raise error("Check-ins are off in kids mode.")
        raise error(f"{feature.title()} is off for this pal. Switch it on in the pal's settings first.")


def ensure_schema(conn):
    conn.executescript(SCHEMA_SQL)


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def _iso(value):
    return datetime.fromtimestamp(value, UTC).isoformat() if value is not None else None


def invalidate_owner(conn, owner, reason='Saved context changed.'):
    conn.execute("UPDATE presence_jobs SET state='stale',error=? WHERE avatar_id=?", (reason, owner or ''))
    conn.execute('UPDATE presence_inbox SET invalidated=1,error=? WHERE avatar_id=?', (reason, owner or ''))


def invalidate_session(conn, session_id, reason='The conversation changed.'):
    conn.execute("UPDATE presence_jobs SET state='stale',error=? WHERE session_id=?", (reason, session_id))
    conn.execute('UPDATE presence_inbox SET invalidated=1,error=? WHERE session_id=?', (reason, session_id))


def provider_identity(provider):
    parsed = urlsplit(provider['base_url'])
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise PresenceError('Presence needs a plain HTTP(S) provider endpoint without URL credentials.', 503)
    try:
        local = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        local = parsed.hostname.casefold() == 'localhost'
    fingerprint = _hash({k: provider.get(k) for k in ('provider_key', 'base_url', 'model', 'api_key')})
    return {'provider_key': provider.get('provider_key') or 'custom', 'base_url': provider['base_url'],
            'model': provider['model'], 'network': not local, 'fingerprint': fingerprint}


def _provider():
    from core.llm import get_client
    chosen = get_client()
    return {key: getattr(chosen, key) for key in ('base_url', 'model', 'api_key', 'provider_key')}


def _call(messages, provider, *, timeout=TIMEOUT_SECONDS, max_retries=0, max_tokens=MAX_TOKENS):
    import httpx
    from openai import OpenAI
    # Allow local inference to begin, then bound slow/trickling response bodies.
    deadline = time.monotonic() + max(1., timeout - 1.)
    class BoundedStream(httpx.SyncByteStream):
        def __init__(self, stream):
            self.stream = stream
        def __iter__(self):
            size = 0
            for block in self.stream:
                size += len(block)
                if time.monotonic() >= deadline or size > 65536:
                    raise httpx.ReadTimeout('Presence response exceeded its deadline or size limit.')
                yield block
        def close(self):
            self.stream.close()
    def bound(response):
        response.stream = BoundedStream(response.stream)
    limits = httpx.Timeout(connect=3., read=float(timeout), write=3., pool=1.)
    # Redirects are disabled so a loopback-only approval cannot be redirected off-device.
    with httpx.Client(timeout=limits, follow_redirects=False, trust_env=False, event_hooks={'response': [bound]}) as transport:
        with OpenAI(base_url=provider['base_url'], api_key=provider.get('api_key') or 'local',
                    timeout=limits, max_retries=max_retries, http_client=transport) as client:
            result = client.chat.completions.create(model=provider['model'], messages=messages,
                max_tokens=max_tokens, temperature=.7, stream=False)
    message = result.choices[0].message
    if getattr(message, 'tool_calls', None):
        raise PresenceError('The presence reply requested an unavailable tool.', 503)
    return message.content


class PresenceService:
    def __init__(self, store, *, clock=None, provider_getter=None, context_getter=None, llm=None, recover=True):
        self.store, self.clock = store, clock or (lambda: datetime.now(UTC))
        self.provider_getter, self.context_getter, self.llm = provider_getter or _provider, context_getter, llm or _call
        self.lock = threading.RLock()
        if not recover:
            return  # Snapshot reads use the datastore's prepared schema without recovering jobs.
        with self.connection() as conn:
            ensure_schema(conn)
            conn.execute("UPDATE presence_jobs SET state='failed',error='Altera restarted before this job finished. Check again explicitly.' WHERE state='running' AND process_id!=?", (PROCESS_ID,))

    @contextmanager
    def connection(self):
        conn = self.store._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def now(self):
        moment = self.clock()
        if moment.tzinfo is None:
            raise ValueError('Presence clock must be timezone aware.')
        return moment.astimezone(UTC)

    def scope(self, session_id, epoch=None):
        if type(session_id) is not int or not 0 < session_id < 2 ** 63:
            raise PresenceError('session_id must be a positive integer.', 400)
        session = self.store.get_session(session_id)
        if not session:
            raise PresenceError('Conversation not found.', 404)
        if session.avatar_id:
            from core.companion import get_services
            if not get_services().avatars.get(session.avatar_id):
                raise PresenceError('This companion is unavailable.', 409)
        current = (self.store.get_key('dataset_epoch') or self.store.dataset_epoch())['value']
        if epoch is not None and epoch != current:
            raise PresenceError('Altera’s data changed. Reload before changing presence.', 409)
        return session.avatar_id or '', current

    def _write_scope(self, session_id, epoch, avatar_id=None):
        if not isinstance(epoch, str) or not epoch:
            raise PresenceError('The current dataset_epoch is required.', 400)
        owner, current = self.scope(session_id, epoch)
        if avatar_id is not None and avatar_id != owner:
            raise PresenceError('The conversation changed companion. Reload before changing presence.', 409)
        return owner, current

    def _scope_conn(self, conn, session_id, owner, epoch):
        session = conn.execute("SELECT COALESCE(avatar_id,'') FROM sessions WHERE id=?", (session_id,)).fetchone()
        row = conn.execute("SELECT value FROM user_profile WHERE key='dataset_epoch'").fetchone()
        try:
            valid = bool(session and session[0] == owner and row and json.loads(row[0]).get('value') == epoch)
        except (ValueError, TypeError, AttributeError):
            valid = False
        if not valid:
            raise PresenceError('The presence conversation or dataset changed. Reload before saving.', 409)

    @contextmanager
    def _writing(self):
        from core.state_gate import STATE
        from core.turns import get_turn_service
        with get_turn_service(self.store).lock:
            lease = STATE.enter('altera-presence')
            try:
                with self.lock:
                    yield
            finally:
                lease.release()

    def _pref(self, session_id):
        owner, epoch = self.scope(session_id)
        with self.connection() as conn:
            row = conn.execute('SELECT * FROM presence_preferences WHERE avatar_id=?', (owner,)).fetchone()
        result = {**DEFAULTS, 'session_id': session_id, 'avatar_id': owner, 'dataset_epoch': epoch, 'revision': 0, 'needs_review': False}
        if row:
            result.update(json.loads(row['settings']))
            result.update(enabled=bool(row['enabled']), revision=row['revision'], needs_review=bool(row['needs_review']))
            if row['session_id'] != session_id or row['dataset_epoch'] != epoch:
                result.update(enabled=False, needs_review=True)
        return result

    def preferences(self, session_id):
        pref = self._pref(session_id)
        try:
            provider = provider_identity(self.provider_getter())
            provider['approved'] = not provider['network'] or pref['provider_approval'] == provider['fingerprint']
        except Exception:
            provider = {'provider_key': 'unavailable', 'base_url': '', 'model': '', 'network': True,
                        'fingerprint': None, 'approved': False, 'error': 'Correct the selected model connection before enabling presence.'}
        return {'preferences': pref, 'provider': provider}

    def update_preferences(self, session_id, epoch, revision, changes, *, avatar_id=None):
        owner, current = self._write_scope(session_id, epoch, avatar_id)
        if not isinstance(changes, dict) or not set(changes) <= set(DEFAULTS):
            raise PresenceError('Unknown presence preference.', 400)
        with self._writing():
            old = self._pref(session_id)
            if type(revision) is not int or revision != old['revision']:
                raise PresenceError('Reload the current presence preferences.')
            settings = {key: changes.get(key, old[key]) for key in DEFAULTS}
            for key in ('enabled', 'quiet_enabled'):
                if type(settings[key]) is not bool:
                    raise PresenceError(key + ' must be true or false.', 400)
            if settings['enabled']:
                _pal_allows(owner, 'presence', PresenceError)
            schedule.timezone(settings['timezone'])
            for key, low, high in (('quiet_start', 0, 1439), ('quiet_end', 0, 1439), ('interval_minutes', 15, 1440),
                    ('cooldown_minutes', 15, 1440), ('daily_cap', 0, 5), ('expiry_minutes', 15, 1440)):
                if type(settings[key]) is not int or not low <= settings[key] <= high:
                    raise PresenceError(f'{key} must be an integer from {low} through {high}.', 400)
            provider = provider_identity(self.provider_getter()) if settings['enabled'] or settings['provider_approval'] is not None else None
            if settings['provider_approval'] is not None and settings['provider_approval'] != provider['fingerprint']:
                raise PresenceError('Approve only the currently selected provider and model.', 400)
            if settings['enabled'] and provider['network'] and settings['provider_approval'] != provider['fingerprint']:
                raise PresenceError('Explicitly approve this external provider before enabling presence.')
            now = self.now().timestamp()
            with self.connection() as conn:
                conn.execute('BEGIN IMMEDIATE')
                self._scope_conn(conn, session_id, owner, current)
                actual = conn.execute('SELECT revision FROM presence_preferences WHERE avatar_id=?', (owner,)).fetchone()
                if (actual[0] if actual else 0) != revision:
                    raise PresenceError('Presence preferences changed. Reload them.')
                conn.execute('INSERT OR REPLACE INTO presence_preferences VALUES(?,?,?,?,?,?,?,?,?)',
                    (owner, session_id, current, revision + 1, int(settings['enabled']), json.dumps(settings), 0,
                     now + settings['interval_minutes'] * 60, now))
                invalidate_owner(conn, owner, 'Presence preferences changed.')
            return self.preferences(session_id)

    def _context(self, session_id, owner, boundary):
        if self.context_getter:
            return self.context_getter(session_id, owner, boundary)
        from core.companion import get_services
        from core.config import get_personality
        from core.knowledge import can_recall, reference
        from core.memory import get_memory_manager
        from core.notes import get_notes_service
        from core import safety
        avatar = get_services().avatars.get(owner) if owner else None
        notes = get_notes_service().store.read(owner) if owner else {}
        collection = get_memory_manager().store.collection
        raw = collection.get(limit=32, **({'where': {'avatar_id': owner}} if owner else {}))
        memories = []
        for identifier, content, metadata in zip(raw.get('ids', []), raw.get('documents', []), raw.get('metadatas', [])):
            metadata = metadata or {}
            candidate = {'id': identifier, 'content': (content or '')[:800], 'metadata': metadata}
            if (((metadata.get('avatar_id') or '') == owner) and len(metadata.get('source_refs') or '') <= 8192
                    and can_recall(candidate, self.store) and not safety.blocks_message(candidate['content'])):
                memories.append({'id': identifier, 'content': candidate['content'],
                                 'metadata': {k: metadata.get(k) for k in ('updated_at', 'source_refs', 'user_context')}})
            if len(memories) == 8:
                break
        memories.sort(key=lambda m: str(m['id']))
        records = [m for m in self.store.get_messages(session_id, limit=20) if m.role == 'user' and m.id > boundary][-6:]
        return {'personality': get_personality(), 'avatar': avatar,
                'notes': {k: str(notes.get(k) or '')[:3000] for k in ('about_you', 'our_story')}, 'memories': memories,
                'sources': [reference(m) for m in records], 'conversation': '\n'.join(m.content for m in records)[-3000:]}

    def _capture(self, session_id):
        from core.knowledge import dirty, floor
        owner, epoch = self.scope(session_id)
        if dirty(self.store, owner):
            raise PresenceError('Repair changed conversation knowledge before presence can use it.')
        pref, boundary = self._pref(session_id), floor(self.store, owner)
        context = self._context(session_id, owner, boundary)
        provider = self.provider_getter()
        identity = provider_identity(provider)
        snapshot = {'avatar_id': owner, 'session_id': session_id, 'dataset_epoch': epoch, 'preferences_revision': pref['revision'],
                    'knowledge_revision': int((self.store.get_key(f'knowledge_dirty:{session_id}') or {}).get('revision') or 0),
                    'recall_after': boundary, 'sources': context.get('sources', []), 'provider_fingerprint': identity['fingerprint'],
                    'context_fingerprint': _hash({k: context.get(k) for k in ('personality', 'avatar', 'notes', 'memories')})}
        return snapshot, context, provider

    def _valid(self, snapshot, fresh=False, current=None):
        try:
            if current is None:
                current, _, _ = self._capture(snapshot['session_id'])
            if not current:
                return False
            if any(current[k] != snapshot[k] for k in current if k != 'sources'):
                return False
            if fresh and current['sources'] != snapshot['sources']:
                return False
            from core.knowledge import reference
            for ref in snapshot['sources']:
                message = self.store.get_message(ref['message_id'])
                if not message or message.role != 'user' or message.session_id != snapshot['session_id'] or message.id <= snapshot['recall_after'] or reference(message) != ref:
                    return False
            return True
        except Exception:
            return False

    def _snapshot_valid(self, raw, fresh=False, current=None):
        try:
            snapshot = json.loads(raw)
            return isinstance(snapshot, dict) and self._valid(snapshot, fresh=fresh, current=current)
        except (TypeError, ValueError):
            return False

    def _allowed(self, pref, now):
        _pal_allows(pref['avatar_id'], 'presence', PresenceError)
        if not pref['enabled'] or pref['needs_review']:
            raise PresenceError('Presence is off. Enable it explicitly for this companion.')
        if schedule.in_quiet(now, pref['timezone'], pref['quiet_start'], pref['quiet_end'], pref['quiet_enabled']):
            raise PresenceError('Presence is paused during quiet hours.')
        identity = provider_identity(self.provider_getter())
        if identity['network'] and pref['provider_approval'] != identity['fingerprint']:
            raise PresenceError('This external provider has not been approved for presence.')
        day = schedule.local_day(now, pref['timezone'])
        start = schedule.occurrence(day, pref['timezone'], 0).timestamp()
        end = schedule.occurrence(day + timedelta(days=1), pref['timezone'], 0).timestamp()
        with self.connection() as conn:
            count = conn.execute('SELECT count(*) FROM presence_inbox WHERE avatar_id=? AND created_at>=? AND created_at<?', (pref['avatar_id'], start, end)).fetchone()[0]
            last = conn.execute('SELECT max(created_at) FROM presence_inbox WHERE avatar_id=?', (pref['avatar_id'],)).fetchone()[0]
        if count >= pref['daily_cap']:
            raise PresenceError('The companion’s daily presence limit has been reached.')
        if last is not None and now.timestamp() - last < pref['cooldown_minutes'] * 60:
            raise PresenceError('Presence is cooling down between messages.')

    def _enqueue(self, session_id, kind, occurrence, due_at, ritual_id=None, *, expected=None):
        snapshot, _, _ = self._capture(session_id)
        if expected is not None and (snapshot['avatar_id'], snapshot['dataset_epoch']) != expected:
            raise PresenceError('The presence conversation or dataset changed before queuing.', 409)
        pref = self._pref(session_id)
        job_id = 'pj_' + _hash([snapshot['avatar_id'], session_id, snapshot['dataset_epoch'], pref['revision'], kind, ritual_id, occurrence])[:32]
        now = self.now().timestamp()
        with self.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            self._scope_conn(conn, session_id, snapshot['avatar_id'], snapshot['dataset_epoch'])
            pending = conn.execute("SELECT id FROM presence_jobs WHERE avatar_id=? AND state IN ('pending','running') AND expires_at>?", (snapshot['avatar_id'], now)).fetchone()
            if pending and pending[0] != job_id:
                raise PresenceError('This companion already has a queued presence message.')
            conn.execute('INSERT OR IGNORE INTO presence_jobs(id,avatar_id,session_id,dataset_epoch,kind,ritual_id,occurrence,due_at,expires_at,state,snapshot,created_at,updated_at) '
                'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)', (job_id, snapshot['avatar_id'], session_id, snapshot['dataset_epoch'], kind, ritual_id, occurrence,
                due_at, due_at + pref['expiry_minutes'] * 60, 'pending', json.dumps(snapshot), now, now))
        return self.job(job_id, session_id)

    def check_now(self, session_id, epoch, request_id, *, avatar_id=None):
        owner, current = self._write_scope(session_id, epoch, avatar_id)
        if not isinstance(request_id, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{8,100}', request_id):
            raise PresenceError('A stable request_id is required.', 400)
        with self._writing():
            # A lost HTTP response must still resolve the original occurrence
            # after publication, cooldown, or a later preference invalidation.
            with self.connection() as conn:
                previous = conn.execute('SELECT id FROM presence_jobs WHERE avatar_id=? AND session_id=? AND dataset_epoch=? AND occurrence=? ORDER BY created_at LIMIT 1',
                                        (owner, session_id, current, 'manual:' + request_id)).fetchone()
            if previous:
                return self.job(previous[0], session_id)
            self._allowed(self._pref(session_id), self.now())
            return self._enqueue(session_id, 'checkin', 'manual:' + request_id, self.now().timestamp(), expected=(owner, current))

    def job(self, job_id, session_id):
        owner, epoch = self.scope(session_id)
        with self.connection() as conn:
            row = conn.execute('SELECT * FROM presence_jobs WHERE id=? AND session_id=? AND avatar_id=?', (job_id, session_id, owner)).fetchone()
        if not row:
            raise PresenceError('Presence job not found.', 404)
        result = {**{k: row[k] for k in ('id', 'session_id', 'avatar_id', 'dataset_epoch', 'kind', 'state', 'error', 'attempts')},
                'due_at': _iso(row['due_at']), 'expires_at': _iso(row['expires_at'])}
        if row['dataset_epoch'] != epoch:
            result.update(state='stale', error='This work belongs to an older dataset.')
        return result

    def _busy(self):
        from core.state_gate import STATE, request_lease
        from core.notes import get_notes_service
        own = request_lease()
        with STATE.lock:
            labels = [label for key, label in STATE.active.items() if key != getattr(own, 'key', None)]
            if STATE.maintenance or STATE.fault or any(label not in {'altera-presence', 'altera-presence-cycle', 'altera-reflection-cycle', 'altera-presence-plan', 'altera-reflection-plan'} for label in labels):
                return True
        notes = get_notes_service()
        with notes._lock:
            return bool(notes._busy)

    def _prompt(self, context, kind):
        from core.prompts import PersonaSection, RulesSection
        from core import safety
        system = PersonaSection(context['personality']).build(context.get('avatar'))[:3200] + '\n' + RulesSection(context['personality']).build()
        from core.expression import get_expression, prompt_section
        owner = (context.get('avatar') or {}).get('id') or ''
        system += prompt_section(get_expression(self.store, owner, now=self.now()))
        system += ('\nWrite a brief, optional presence message for the private inbox, at most two sentences. '
                   'No tools, photos, voice tags, demands, guilt, invented shared history or claims of missing the person. '
                   'You have no needs or hurt about their absence. Use only supplied evidence; uncertain notes are summaries, not proof. '
                   'If context is sparse, a simple warm greeting is enough. Never imply a fictional persona biography is the user’s real life.')
        text = json.dumps({'occasion': kind, 'recent_user_words': context.get('conversation', ''),
                           'derived_notes': {k: str(context.get('notes', {}).get(k) or '')[:800] for k in ('about_you', 'our_story')},
                           'eligible_memories': [m['content'][:400] for m in context.get('memories', [])[:4]]}, ensure_ascii=False)[:7000]
        if safety.blocks_message(text):
            raise PresenceError('This context cannot be used for a presence message.', 422)
        return [{'role': 'system', 'content': system}, {'role': 'user', 'content': text}]

    def run_one(self):
        if not _generation_lock.acquire(blocking=False):
            return False
        lease = None
        try:
            if self._busy():
                return False
            from core.state_gate import STATE
            from core.turns import get_turn_service
            with get_turn_service(self.store).lock:
                if self._busy():
                    return False
                lease = STATE.enter('altera-presence')
                with self.lock, self.connection() as conn:
                    conn.execute('BEGIN IMMEDIATE')
                    now = self.now().timestamp()
                    conn.execute("UPDATE presence_jobs SET state='expired',error='This occurrence expired.' WHERE state='pending' AND expires_at<=?", (now,))
                    if conn.execute("SELECT 1 FROM presence_jobs WHERE state='running'").fetchone():
                        return False
                    if conn.execute("SELECT 1 FROM reflection_jobs WHERE state='running'").fetchone():
                        return False
                    row = conn.execute("SELECT * FROM presence_jobs WHERE state='pending' AND due_at<=? ORDER BY due_at DESC,id LIMIT 1", (now,)).fetchone()
                    if not row:
                        return False
                    job, token = dict(row), uuid.uuid4().hex
                    conn.execute("UPDATE presence_jobs SET state='running',process_id=?,claim_token=?,attempts=attempts+1,updated_at=? WHERE id=?",
                                 (PROCESS_ID, token, now, job['id']))
            try:
                snapshot = json.loads(job['snapshot'])
                self._allowed(self._pref(job['session_id']), self.now())
                if not self._valid(snapshot, fresh=True):
                    raise PresenceError('The source context changed before generation.')
                _, context, provider = self._capture(job['session_id'])
                text = self.llm(self._prompt(context, job['kind']), provider, timeout=TIMEOUT_SECONDS, max_retries=0, max_tokens=MAX_TOKENS)
                text = re.sub(r'<think>.*?(?:</think>|$)', '', text or '', flags=re.S | re.I).strip()
                from core import safety
                if not text or len(text) > 2000 or '[[photo:' in text or safety.blocks_message(text):
                    raise PresenceError('The provider did not return a usable presence message.', 503)
                with get_turn_service(self.store).lock, self.lock:
                    self._allowed(self._pref(job['session_id']), self.now())
                    if self._busy() or not self._valid(snapshot, fresh=True) or self.now().timestamp() >= job['expires_at']:
                        raise PresenceError('The source context or delivery conditions changed during generation.')
                    with self.connection() as conn:
                        conn.execute('BEGIN IMMEDIATE')
                        current = conn.execute('SELECT state,claim_token FROM presence_jobs WHERE id=?', (job['id'],)).fetchone()
                        if not current or current['state'] != 'running' or current['claim_token'] != token:
                            raise PresenceError('This presence job was invalidated.')
                        now = self.now().timestamp()
                        conn.execute('INSERT INTO presence_inbox(id,job_id,avatar_id,session_id,dataset_epoch,kind,text,created_at,expires_at,snapshot) VALUES(?,?,?,?,?,?,?,?,?,?)',
                            ('pi_' + job['id'][3:], job['id'], job['avatar_id'], job['session_id'], job['dataset_epoch'], job['kind'], text, now, job['expires_at'], job['snapshot']))
                        conn.execute("UPDATE presence_jobs SET state='published',updated_at=? WHERE id=?", (now, job['id']))
            except Exception as error:
                safe = str(error) if isinstance(error, PresenceError) else 'Presence generation failed. No fallback provider was used.'
                with self.connection() as conn:
                    conn.execute("UPDATE presence_jobs SET state=CASE WHEN state='stale' THEN 'stale' ELSE 'failed' END,error=?,updated_at=? WHERE id=? AND claim_token=?",
                                 (safe, self.now().timestamp(), job['id'], token))
            return True
        finally:
            if lease:
                lease.release()
            _generation_lock.release()

    def _item(self, row, current=None):
        invalid = bool(row['invalidated']) or not self._snapshot_valid(row['snapshot'], current=current)
        expired = row['accepted_message_id'] is None and self.now().timestamp() >= row['expires_at']
        state = 'dismissed' if row['dismissed_at'] else ('stale' if invalid else ('accepted' if row['accepted_message_id'] else ('expired' if expired else 'available')))
        return {**{k: row[k] for k in ('id', 'job_id', 'session_id', 'avatar_id', 'dataset_epoch', 'kind', 'accepted_message_id')},
                'text': row['text'] if state in {'available', 'accepted'} else '', 'state': state,
                'error': row['error'] or ('The saved context changed.' if invalid else ('This item expired.' if expired else '')),
                **{k: _iso(row[k]) for k in ('created_at', 'expires_at', 'read_at', 'dismissed_at', 'accepted_at')}}

    def inbox(self, session_id):
        owner, epoch = self.scope(session_id)
        with self.connection() as conn:
            rows = conn.execute('SELECT * FROM presence_inbox WHERE avatar_id=? AND session_id=? ORDER BY created_at DESC LIMIT 100', (owner, session_id)).fetchall()
            jobs = conn.execute('SELECT id FROM presence_jobs WHERE avatar_id=? AND session_id=? ORDER BY created_at DESC LIMIT 20', (owner, session_id)).fetchall()
        from core.expression import get_expression
        current = {}
        if rows:
            try:
                current, _, _ = self._capture(session_id)
            except Exception:
                pass  # Unavailable knowledge means every derived item is stale.
        return {'scope': {'session_id': session_id, 'avatar_id': owner, 'dataset_epoch': epoch},
                'items': [self._item(dict(row), current=current) for row in rows], 'jobs': [self.job(r[0], session_id) for r in jobs],
                'expression': get_expression(self.store, owner, now=self.now())}

    def notices(self, after=None, *, include_text=False, limit=20):
        """Check-ins that arrived after `after` (seconds since 1970) and are still waiting unread, across everyone, for the
        browser's own notifications. Reads only. Never a kids pal's, never one from an older dataset or one whose context
        changed. Without `after` nothing is listed: the browser starts watching from `now`. The text is only included when
        asked for (the person chose to see it in the notification)."""
        now = self.now().timestamp()
        if after is None:
            return {'now': now, 'notices': []}
        epoch = (self.store.get_key('dataset_epoch') or self.store.dataset_epoch())['value']
        with self.connection() as conn:
            rows = conn.execute(
                'SELECT id,avatar_id,session_id,kind,text,created_at FROM presence_inbox WHERE created_at>? AND expires_at>? '
                'AND read_at IS NULL AND dismissed_at IS NULL AND accepted_message_id IS NULL AND invalidated=0 '
                'AND dataset_epoch=? ORDER BY created_at DESC LIMIT ?', (after, now, epoch, limit)).fetchall()
        from core import kids
        from core.companion import get_services
        found = []
        for row in rows:
            record = get_services().avatars.get(row['avatar_id']) if row['avatar_id'] else None
            if row['avatar_id'] and (not record or kids.is_on(record)):
                continue
            notice = {'id': row['id'], 'avatar_id': row['avatar_id'], 'session_id': row['session_id'], 'kind': row['kind'],
                      'name': (record or {}).get('name') or 'Altera', 'created_at': row['created_at']}
            if include_text:
                notice['text'] = row['text']
            found.append(notice)
        return {'now': now, 'notices': found}

    def action(self, item_id, action, session_id, epoch, *, avatar_id=None):
        if action not in {'read', 'dismiss', 'accept'}:
            raise PresenceError('Unknown inbox action.', 400)
        owner, current = self._write_scope(session_id, epoch, avatar_id)
        with self._writing(), self.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            self._scope_conn(conn, session_id, owner, current)
            row = conn.execute('SELECT * FROM presence_inbox WHERE id=? AND avatar_id=? AND session_id=?', (item_id, owner, session_id)).fetchone()
            if not row:
                raise PresenceError('Inbox item not found for this conversation.', 404)
            item = self._item(dict(row))
            if item['state'] in {'stale', 'expired'} and action != 'dismiss':
                raise PresenceError('This inbox item is no longer current.', 410)
            now = self.now().timestamp()
            if action == 'accept' and not row['accepted_message_id']:
                if row['dismissed_at'] or self._busy():
                    raise PresenceError('Wait for active work to finish before accepting this item.')
                cursor = conn.execute("INSERT INTO messages(session_id,role,content) VALUES(?,'assistant',?)", (session_id, row['text']))
                conn.execute('UPDATE sessions SET updated_at=CURRENT_TIMESTAMP WHERE id=?', (session_id,))
                conn.execute('UPDATE presence_inbox SET accepted_message_id=?,accepted_at=?,read_at=COALESCE(read_at,?) WHERE id=?', (cursor.lastrowid, now, now, item_id))
            elif action in {'read', 'dismiss'}:
                column = 'read_at' if action == 'read' else 'dismissed_at'
                conn.execute(f'UPDATE presence_inbox SET {column}=COALESCE({column},?) WHERE id=?', (now, item_id))
            updated = conn.execute('SELECT * FROM presence_inbox WHERE id=?', (item_id,)).fetchone()
            return self._item(dict(updated))

    def rituals(self, session_id):
        owner, epoch = self.scope(session_id)
        with self.connection() as conn:
            rows = conn.execute('SELECT * FROM presence_rituals WHERE avatar_id=? AND session_id=? AND dataset_epoch=? AND deleted=0 ORDER BY created_at,id', (owner, session_id, epoch)).fetchall()
        return [{k: r[k] for k in ('id', 'session_id', 'avatar_id', 'dataset_epoch', 'revision', 'kind', 'minute', 'weekday', 'enabled')} for r in rows]

    def ritual(self, session_id, epoch, changes, ritual_id=None, revision=None, delete=False, *, avatar_id=None):
        owner, current = self._write_scope(session_id, epoch, avatar_id)
        if not isinstance(changes, dict) or not set(changes) <= {'kind', 'minute', 'weekday', 'enabled'}:
            raise PresenceError('Unknown ritual field.', 400)
        with self._writing(), self.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            old = conn.execute('SELECT * FROM presence_rituals WHERE id=? AND avatar_id=? AND session_id=?', (ritual_id, owner, session_id)).fetchone() if ritual_id else None
            self._scope_conn(conn, session_id, owner, current)
            if ritual_id and (not old or type(revision) is not int or revision != old['revision']):
                raise PresenceError('Reload the current ritual before changing it.')
            data = {**({'kind': old['kind'], 'minute': old['minute'], 'weekday': old['weekday'], 'enabled': bool(old['enabled'])} if old else {'kind': 'greeting', 'minute': 540, 'weekday': None, 'enabled': True}), **changes}
            if data['kind'] not in {'greeting', 'goodnight', 'weekly'} or type(data['minute']) is not int or not 0 <= data['minute'] < 1440 or type(data['enabled']) is not bool:
                raise PresenceError('Choose a valid ritual kind, minute and enabled flag.', 400)
            if data['kind'] == 'weekly' and (type(data['weekday']) is not int or not 0 <= data['weekday'] <= 6):
                raise PresenceError('A weekly ritual needs weekday 0 through 6.', 400)
            identifier = ritual_id or 'pr_' + uuid.uuid4().hex
            conn.execute('INSERT OR REPLACE INTO presence_rituals VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (identifier, owner, session_id, current, (old['revision'] if old else 0) + 1, data['kind'], data['minute'],
                 data['weekday'] if data['kind'] == 'weekly' else None, int(data['enabled']), int(delete), old['created_at'] if old else self.now().timestamp()))
            invalidate_owner(conn, owner, 'A presence ritual changed.')
        return next((r for r in self.rituals(session_id) if r['id'] == identifier), {'id': identifier, 'deleted': True})

    def tick(self):
        from core.state_gate import STATE
        lease = STATE.enter('altera-presence-plan')
        try:
            with self.lock:
                self._plan()
        finally:
            lease.release()

    def _plan(self):
        """Plan at most one catch-up occurrence per companion; perform no model work."""
        now = self.now()
        with self.connection() as conn:
            rows = conn.execute('SELECT * FROM presence_preferences WHERE enabled=1 AND needs_review=0 ORDER BY avatar_id LIMIT 100').fetchall()
        for row in rows:
            try:
                pref = self._pref(row['session_id'])
                self._allowed(pref, now)
                candidates = []
                with self.connection() as conn:
                    last_message = conn.execute('SELECT max(timestamp) FROM messages WHERE session_id=?', (row['session_id'],)).fetchone()[0]
                last_activity = 0
                if last_message:
                    moment = datetime.fromisoformat(last_message)
                    last_activity = (moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment).timestamp()
                if now.timestamp() - last_activity < 15 * 60:
                    continue
                regular_due = max(row['next_due_at'], last_activity + pref['interval_minutes'] * 60)
                if regular_due <= now.timestamp():
                    interval = pref['interval_minutes'] * 60
                    due = regular_due + int((now.timestamp() - regular_due) // interval) * interval
                    candidates.append((due, 'checkin', None, str(int(due))))
                    with self.connection() as conn:
                        conn.execute('UPDATE presence_preferences SET next_due_at=? WHERE avatar_id=? AND revision=?', (due + interval, row['avatar_id'], row['revision']))
                for ritual in self.rituals(row['session_id']):
                    day = schedule.local_day(now, pref['timezone'])
                    # The largest expiry is one day: include yesterday so a
                    # restart just after midnight can catch a recent goodnight.
                    for target in (day - timedelta(days=1), day):
                        due = schedule.occurrence(target, pref['timezone'], ritual['minute']).timestamp()
                        if ritual['enabled'] and (ritual['kind'] != 'weekly' or target.weekday() == ritual['weekday']) and due <= now.timestamp():
                            candidates.append((due, ritual['kind'], ritual['id'], f"{ritual['revision']}:{target.isoformat()}"))
                candidates = [c for c in candidates if now.timestamp() < c[0] + pref['expiry_minutes'] * 60]
                if candidates:
                    due, kind, ritual_id, occurrence = max(candidates)
                    self._enqueue(row['session_id'], kind, occurrence, due, ritual_id,
                                  expected=(row['avatar_id'], row['dataset_epoch']))
            except ValueError:
                continue

    def clear_owner(self, owner):
        with self.lock, self.connection() as conn:
            invalidate_owner(conn, owner, 'Presence data was cleared.')
            conn.execute("UPDATE presence_inbox SET text='',snapshot='{}' WHERE avatar_id=?", (owner or '',))
            conn.execute("UPDATE presence_jobs SET snapshot='{}' WHERE avatar_id=?", (owner or '',))
            conn.execute('DELETE FROM presence_preferences WHERE avatar_id=?', (owner or '',))
            conn.execute('UPDATE presence_rituals SET deleted=1,enabled=0 WHERE avatar_id=?', (owner or '',))

    def clear_session(self, session_id):
        with self.lock, self.connection() as conn:
            invalidate_session(conn, session_id, 'Presence data was cleared.')
            conn.execute("UPDATE presence_inbox SET text='',snapshot='{}' WHERE session_id=?", (session_id,))
            conn.execute("UPDATE presence_jobs SET snapshot='{}' WHERE session_id=?", (session_id,))
            conn.execute('DELETE FROM presence_preferences WHERE session_id=?', (session_id,))
            conn.execute('UPDATE presence_rituals SET deleted=1,enabled=0 WHERE session_id=?', (session_id,))

    def clear_all(self):
        with self.connection() as conn:
            owners = {r[0] for r in conn.execute('SELECT avatar_id FROM presence_jobs UNION SELECT avatar_id FROM presence_preferences UNION SELECT avatar_id FROM presence_inbox')}
        for owner in owners:
            self.clear_owner(owner)


_service_lock = threading.Lock()


def get_presence_service(store=None):
    from core.datastore import get_datastore
    store = store or get_datastore()
    with _service_lock:
        if not hasattr(store, '_presence_service'):
            store._presence_service = PresenceService(store)
        return store._presence_service


def filter_messages(store, session_id, messages, *, exclude_derived=False, snapshot=False):
    """Keep visible chat history, but exclude invalid accepted derived messages."""
    if snapshot:
        service = getattr(store, '_presence_service', None) or PresenceService(store, recover=False)
    else:
        service = get_presence_service(store)
    rows = []
    # Split/merge moves messages without changing IDs. Original session linkage
    # must never let a stale derived reply escape the source boundary.
    identifiers = [message.id for message in messages]
    with service.connection() as conn:
        for offset in range(0, len(identifiers), 400):
            batch = identifiers[offset:offset + 400]
            rows.extend(conn.execute('SELECT * FROM presence_inbox WHERE accepted_message_id IN (' + ','.join('?' for _ in batch) + ')', batch).fetchall())
    excluded = {row['accepted_message_id'] for row in rows if exclude_derived or row['invalidated'] or not service._snapshot_valid(row['snapshot'])}
    return [message for message in messages if message.id not in excluded]


def start_scheduler(app, *, interval=30., before_cycle=None, register=None):
    """Start only when explicitly requested, with a joinable owned lifetime."""
    from core.scheduler import start_scheduler as start_owned
    return start_owned(app, key='altera_presence_scheduler', name='altera-presence-scheduler', label='altera-presence-cycle',
                       service=get_presence_service, logger=LOGGER, interval=interval,
                       before_cycle=before_cycle, register=register)
