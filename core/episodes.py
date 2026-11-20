"""Source-backed companion episodes and explicit story curation, without inference.

An actual_exchange records words exchanged, not verified real-world biography.
Imagined scenes and derived summaries retain their labels in every recall input.
Source text is sliced deterministically on reads; it is never copied into evidence.
"""
from contextlib import contextmanager
from datetime import datetime, UTC
import hashlib
import json
import math
import re
from types import SimpleNamespace
import uuid

from core.knowledge import prompt_messages
from core.salience import describe as describe_salience, lexical_relevance

KINDS = {'actual_exchange', 'imagined_scene', 'derived_summary'}
MAX_SOURCES = 2
MAX_EXCERPT = 2000
MAX_SOURCE_CHARS = 4000
HALF_LIFE_DAYS = 30
MAX_RECALL_CHARS = 1800
LABELS = {'actual_exchange': 'Actual textual exchange; statements are attributed, not verified biography.',
          'imagined_scene': 'Imagined scene; never a real shared event or factual user biography.',
          'derived_summary': 'Derived summary; interpretation, not independent evidence or verified biography.'}


class EpisodeError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def ensure_schema(conn):
    # Individual statements preserve a caller's transaction (executescript commits).
    conn.execute('''CREATE TABLE IF NOT EXISTS companion_episodes (
        id TEXT PRIMARY KEY, owner TEXT NOT NULL, session_id INTEGER NOT NULL,
        dataset_epoch TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
        kind TEXT NOT NULL, title TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '',
        uncertainty TEXT NOT NULL DEFAULT '', confidence REAL, salience INTEGER NOT NULL DEFAULT 3,
        pinned INTEGER NOT NULL DEFAULT 0, in_story INTEGER NOT NULL DEFAULT 0,
        curation TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'current', reason TEXT NOT NULL DEFAULT '',
        source_refs TEXT NOT NULL, recall_floor INTEGER NOT NULL, knowledge_revision INTEGER NOT NULL,
        capture_key TEXT NOT NULL, capture_fingerprint TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(owner,dataset_epoch,capture_key))''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_episodes_owner ON companion_episodes(owner,created_at)')
    conn.execute('''CREATE TABLE IF NOT EXISTS episode_recall_turns (
        user_message_id INTEGER PRIMARY KEY, assistant_message_id INTEGER NOT NULL UNIQUE,
        owner TEXT NOT NULL, dataset_epoch TEXT NOT NULL, episode_ids TEXT NOT NULL,
        used_at TEXT NOT NULL)''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_episode_recall_owner ON episode_recall_turns(owner,dataset_epoch,used_at)')
    conn.execute('''CREATE TABLE IF NOT EXISTS episode_recall_reservations (
        request_id TEXT PRIMARY KEY, owner TEXT NOT NULL, session_id INTEGER NOT NULL,
        dataset_epoch TEXT NOT NULL, process_id TEXT NOT NULL, episode_ids TEXT NOT NULL,
        snapshot_sha256 TEXT NOT NULL, UNIQUE(owner,dataset_epoch))''')


def invalidate_owner(conn, owner, reason='Saved context changed.'):
    conn.execute("UPDATE companion_episodes SET status='stale',reason=? WHERE owner=? AND status='current'",
                 (reason, owner or ''))
    conn.execute('DELETE FROM episode_recall_reservations WHERE owner=?', (owner or '',))


def invalidate_session(conn, session_id, reason='Conversation changed.'):
    # Source IDs may move, so original and current source sessions both matter.
    rows = conn.execute("SELECT id,session_id,source_refs FROM companion_episodes WHERE status='current'").fetchall()
    for row in rows:
        refs = _refs(row['source_refs'])
        if row['session_id'] == session_id or any(r.get('session_id') == session_id for r in refs):
            conn.execute("UPDATE companion_episodes SET status='stale',reason=? WHERE id=?", (reason, row['id']))
    # Any changed knowledge in this owner invalidates a pending prompt selection.
    conn.execute("DELETE FROM episode_recall_reservations WHERE session_id=? OR owner IN (SELECT COALESCE(avatar_id,'') FROM sessions WHERE id=?)", (session_id, session_id))


def invalidate_all(conn, reason='Restored data needs source review.'):
    conn.execute("UPDATE companion_episodes SET status='stale',reason=? WHERE status='current'", (reason,))
    conn.execute('DELETE FROM episode_recall_turns')
    conn.execute('DELETE FROM episode_recall_reservations')


def delete_owner(conn, owner):
    conn.execute('DELETE FROM companion_episodes WHERE owner=?', (owner or '',))
    conn.execute('DELETE FROM episode_recall_turns WHERE owner=?', (owner or '',))
    conn.execute('DELETE FROM episode_recall_reservations WHERE owner=?', (owner or '',))


def delete_session(conn, session_id):
    invalidate_session(conn, session_id, 'Source conversation removed.')
    conn.execute('DELETE FROM companion_episodes WHERE session_id=?', (session_id,))


def clear_all(conn):
    conn.execute('DELETE FROM companion_episodes')
    conn.execute('DELETE FROM episode_recall_turns')
    conn.execute('DELETE FROM episode_recall_reservations')


def _refs(value):
    try:
        result = json.loads(value)
        if isinstance(result, list) and 0 < len(result) <= MAX_SOURCES and all(isinstance(r, dict) for r in result):
            return result
    except (TypeError, ValueError):
        pass
    return []


def _hash(text):
    try:
        return hashlib.sha256(text.encode('utf-8')).hexdigest()
    except UnicodeEncodeError:
        raise EpisodeError('Episode text contains invalid Unicode.') from None


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _integer(value, label, minimum=1, maximum=2 ** 63 - 1):
    if type(value) is not int or not minimum <= value <= maximum:
        raise EpisodeError(f'{label} must be an integer from {minimum} to {maximum}.')
    return value


def _text(value, label, maximum, *, empty=False):
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        raise EpisodeError(f'{label} must be text of at most {maximum} characters.')
    if any(ord(c) < 32 and c not in '\n\r\t' for c in value):
        raise EpisodeError(f'{label} contains unsupported control characters.')
    try:
        value.encode('utf-8')
    except UnicodeEncodeError:
        raise EpisodeError(f'{label} contains invalid Unicode.') from None
    from core.safety import blocks_message
    if blocks_message(value):
        raise EpisodeError('That content cannot be retained as an episode.', 422)
    return value.strip()


def _key(conn, name):
    row = conn.execute('SELECT value FROM user_profile WHERE key=?', (name,)).fetchone()
    try:
        result = json.loads(row[0]) if row else {}
        if isinstance(result, dict):
            return result
    except (TypeError, ValueError):
        pass
    raise EpisodeError('Saved knowledge context is unavailable.', 409)


class _SnapshotView:
    """Run the shared evidence filter on the caller's SQLite snapshot, read-only."""
    def __init__(self, conn):
        self.conn = conn
        self._presence_service = SimpleNamespace(connection=self.connection)

    @contextmanager
    def connection(self):
        yield self.conn

    def get_key(self, key):
        return _key(self.conn, key)

    def get_session(self, session_id):
        row = self.conn.execute("SELECT id,COALESCE(avatar_id,'') avatar_id FROM sessions WHERE id=?", (session_id,)).fetchone()
        return SimpleNamespace(**dict(row)) if row else None


def _context(conn, owner, session_id):
    session = conn.execute("SELECT COALESCE(avatar_id,'') FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not session or session[0] != owner:
        raise EpisodeError('The source conversation changed owner or is unavailable.', 409)
    epoch = _key(conn, 'dataset_epoch').get('value')
    if not isinstance(epoch, str) or not epoch:
        raise EpisodeError('Dataset identity is unavailable. Reload Altera.', 409)
    boundary = _key(conn, 'knowledge_recall:' + (owner or '@default')).get('through', 0)
    _integer(boundary, 'Recall boundary', 0)
    state = _key(conn, f'knowledge_dirty:{session_id}')
    revision = state.get('revision', 0)
    _integer(revision, 'Knowledge revision', 0)
    pending = bool(state.get('pending')) or bool(_key(conn, 'knowledge_dirty_owner:' + (owner or '@default')).get('pending'))
    for row in conn.execute("SELECT p.value FROM user_profile p JOIN sessions s ON p.key='knowledge_dirty:' || s.id WHERE COALESCE(s.avatar_id,'')=?", (owner,)):
        try:
            pending = pending or bool(json.loads(row[0]).get('pending'))
        except (TypeError, ValueError, AttributeError):
            pending = True
    return epoch, boundary, revision, pending


def _source(conn, owner, spec, *, session_id=None, expected=False):
    if not isinstance(spec, dict) or not set(spec) <= {'message_id', 'session_id', 'role', 'sha256', 'start', 'end'}:
        raise EpisodeError('Each source must contain a message_id and optional character range.')
    identifier = _integer(spec.get('message_id'), 'Source message ID')
    if 'session_id' in spec:
        _integer(spec['session_id'], 'Source session ID')
    if 'sha256' in spec and (not isinstance(spec['sha256'], str) or not re.fullmatch(r'[a-f0-9]{64}', spec['sha256'])):
        raise EpisodeError('Source sha256 must be a lowercase SHA-256 digest.')
    if 'role' in spec and (not isinstance(spec['role'], str) or spec['role'] not in {'user', 'assistant'}):
        raise EpisodeError('Source role must be user or assistant.')
    row = conn.execute("SELECT m.*,COALESCE(s.avatar_id,'') owner FROM messages m JOIN sessions s ON s.id=m.session_id WHERE m.id=?", (identifier,)).fetchone()
    if not row or row['owner'] != owner or (session_id is not None and row['session_id'] != session_id):
        raise EpisodeError('Source message not found in this companion conversation.', 404)
    if session_id is not None and 'session_id' in spec and spec['session_id'] != session_id:
        raise EpisodeError('The reviewed source conversation changed.', 409)
    if row['role'] not in {'user', 'assistant'}:
        raise EpisodeError('Only user and assistant exchange text can be episode evidence.')
    epoch, boundary, revision, pending = _context(conn, owner, row['session_id'])
    if pending or identifier <= boundary:
        raise EpisodeError('Source evidence is outside the current recall boundary or needs repair.', 409)
    message = SimpleNamespace(id=identifier, session_id=row['session_id'], role=row['role'], content=row['content'])
    if not prompt_messages(_SnapshotView(conn), row['session_id'], [message], allow_derived=False):
        raise EpisodeError('Generated inbox/reflection text cannot be fresh episode evidence.', 409)
    from core.safety import blocks_message
    if blocks_message(row['content']):
        raise EpisodeError('That source cannot be retained as episode evidence.', 422)
    start = _integer(spec.get('start', 0), 'Source start', 0, len(row['content']))
    end = _integer(spec.get('end', min(len(row['content']), start + MAX_EXCERPT)), 'Source end', 1, len(row['content']))
    if end <= start or end - start > MAX_EXCERPT:
        raise EpisodeError(f'Each source range must contain 1 to {MAX_EXCERPT} characters.')
    sha = _hash(row['content'])
    if ((expected or 'sha256' in spec) and spec.get('sha256') != sha) or (('role' in spec or expected) and spec.get('role') != row['role']):
        raise EpisodeError('The episode source changed.', 409)
    if 'session_id' in spec and spec['session_id'] != row['session_id'] and not expected:
        raise EpisodeError('The reviewed source conversation changed.', 409)
    return {'message_id': identifier, 'session_id': row['session_id'], 'role': row['role'],
            'sha256': sha, 'start': start, 'end': end}, row['content'][start:end], epoch, boundary, revision


class EpisodeService:
    def __init__(self, store, *, clock=None, knowledge_factory=None, notes_service=None):
        self.store, self.clock = store, clock or (lambda: datetime.now(UTC))
        self.knowledge_factory, self.notes_service = knowledge_factory, notes_service
        # Initialization is explicit; report/preview methods only read afterwards.
        with self.connection(write=True) as conn:
            ensure_schema(conn)
            self._recover_reservations(conn)

    def _recover_reservations(self, conn):
        from core.turns import PROCESS_ID
        epoch = _key(conn, 'dataset_epoch').get('value')
        for row in conn.execute('SELECT * FROM episode_recall_reservations').fetchall():
            turn = conn.execute("SELECT t.*,COALESCE(s.avatar_id,'') owner FROM chat_turns t JOIN sessions s ON s.id=t.session_id WHERE request_id=?", (row['request_id'],)).fetchone()
            if (turn and turn['status'] == 'saved' and turn['process_id'] != PROCESS_ID
                    and row['process_id'] == turn['process_id'] and turn['session_id'] == row['session_id']
                    and turn['owner'] == row['owner'] and row['dataset_epoch'] == epoch):
                try:
                    ids = json.loads(row['episode_ids'])
                    if not isinstance(turn['updated_at'], str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}', turn['updated_at']):
                        raise ValueError('The saved reply time is invalid.')
                    saved_at = datetime.fromisoformat(turn['updated_at']).replace(tzinfo=UTC)
                    if saved_at > self.now():
                        raise ValueError('The saved reply time is in the future.')
                    self._record_recall(conn, row['session_id'], epoch, ids, turn['user_message_id'],
                                        turn['assistant_message_id'], saved_at, request_id=row['request_id'])
                except (EpisodeError, TypeError, ValueError, OverflowError):
                    pass  # Invalid or stale source is never made current by recovery.
                conn.execute('DELETE FROM episode_recall_reservations WHERE request_id=?', (row['request_id'],))
                continue
            if (not turn or turn['status'] not in {'sending', 'streaming'} or
                    turn['process_id'] != PROCESS_ID or row['process_id'] != PROCESS_ID or
                    turn['session_id'] != row['session_id'] or turn['owner'] != row['owner'] or
                    row['dataset_epoch'] != epoch):
                conn.execute('DELETE FROM episode_recall_reservations WHERE request_id=?', (row['request_id'],))

    def now(self, value=None):
        moment = value if value is not None else self.clock()
        if not isinstance(moment, datetime) or moment.tzinfo is None:
            raise EpisodeError('Episode clock must include a timezone.')
        return moment.astimezone(UTC)

    @contextmanager
    def connection(self, *, write=False):
        from core.state_gate import STATE
        lease = STATE.enter('altera-episodes') if write else None
        conn = None
        try:
            conn = self.store._connect()
            with conn:
                conn.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
                yield conn
        finally:
            if conn is not None:
                conn.close()
            if lease:
                lease.release()

    def _scope(self, conn, session_id, epoch=None, *, write=False, expected_owner=None):
        _integer(session_id, 'session_id')
        session = conn.execute("SELECT COALESCE(avatar_id,'') FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not session:
            raise EpisodeError('Conversation not found.', 404)
        owner = session[0]
        if expected_owner is not None and expected_owner != owner:
            raise EpisodeError('The conversation changed companion. Reload before saving.', 409)
        current, boundary, revision, pending = _context(conn, owner, session_id)
        if write and (not isinstance(epoch, str) or not epoch):
            raise EpisodeError('The current dataset_epoch is required.')
        if epoch is not None and epoch != current:
            raise EpisodeError('Altera’s dataset changed. Reload before changing episodes.', 409)
        return {'session_id': session_id, 'avatar_id': owner, 'dataset_epoch': current}, boundary, revision, pending

    def _evidence(self, conn, row):
        result, current = [], row['status'] == 'current'
        refs = _refs(row['source_refs'])
        if not refs:
            return [], False
        for ref in refs:
            item = {'message_id': ref.get('message_id'), 'original_session_id': ref.get('session_id'),
                    'session_id': ref.get('session_id'), 'role': ref.get('role'), 'sha256': ref.get('sha256'),
                    'start': ref.get('start'), 'end': ref.get('end'), 'status': 'unavailable', 'excerpt': ''}
            try:
                source, excerpt, epoch, boundary, revision = _source(conn, row['owner'], ref, expected=True)
                # Moving IDs within this owner retains attribution; current revision must agree.
                valid = epoch == row['dataset_epoch'] and boundary == row['recall_floor']
                if source['session_id'] == row['session_id']:
                    valid = valid and revision == row['knowledge_revision']
                item.update(session_id=source['session_id'], status='current' if valid else 'changed', excerpt=excerpt if valid else '')
                current = current and valid
            except (EpisodeError, TypeError, ValueError, KeyError):
                current = False
            result.append(item)
        return result, current

    def _ranking(self, row, query='', now=None, evidence=''):
        terms = set(re.findall(r'\w{3,}', query.casefold()))
        content = (row['title'] + ' ' + row['summary'] + ' ' + evidence).casefold()
        matched = sorted(term for term in terms if lexical_relevance(term, content) > 0)
        relevance = lexical_relevance(query, content)
        receipt = describe_salience({'salience': row['salience'], 'pinned': bool(row['pinned']),
                                     'created_at': row['created_at']}, now=self.now(now),
                                    similarity=relevance if query else None)
        score = receipt['ranking_score'] if query else receipt['priority_score'] + (.18 if row['pinned'] else 0)
        return {**receipt, 'score': round(score, 4), 'recency': receipt['decay_factor'],
                'relevance': round(relevance, 4), 'matched_terms': matched,
                'pin_boost': .18 if row['pinned'] else 0, 'decay_floor': .2}

    def _item(self, conn, row, query='', now=None):
        sources, current = self._evidence(conn, row)
        return {**{k: row[k] for k in ('id', 'session_id', 'dataset_epoch', 'revision', 'kind', 'confidence',
                'salience', 'curation', 'created_at', 'updated_at')}, 'avatar_id': row['owner'],
                'title': row['title'] if current else '', 'summary': row['summary'] if current else '',
                'uncertainty': row['uncertainty'] if current else '', 'label': LABELS.get(row['kind'], 'Unclassified derived material.'),
                'pinned': bool(row['pinned']), 'in_story': bool(row['in_story']),
                'status': 'current' if current else ('forgotten' if row['status'] == 'forgotten' else 'stale'),
                'reason': row['reason'] or ('' if current else 'Source or knowledge context changed; capture current evidence again.'),
                'eligible': current and row['curation'] != 'reflection', 'sources': sources,
                'ranking': self._ranking(row, query, now, '\n'.join(s['excerpt'] for s in sources) if current else '')}

    def _budget(self, conn, owner, epoch, now=None):
        moment = self.now(now)
        rows = conn.execute('SELECT episode_ids FROM episode_recall_turns WHERE owner=? AND dataset_epoch=? ORDER BY assistant_message_id DESC LIMIT 4', (owner, epoch)).fetchall()
        recent = any(row[0] != '[]' for row in rows)
        today = moment.date().isoformat()
        count = conn.execute("SELECT count(*) FROM episode_recall_turns WHERE owner=? AND dataset_epoch=? AND episode_ids!='[]' AND substr(used_at,1,10)=?", (owner, epoch, today)).fetchone()[0]
        return {'available': not recent and count < 2, 'daily_used': count, 'daily_limit': 2,
                'minimum_turn_spacing': 5, 'preview_spends_budget': False}

    def inventory(self, session_id):
        with self.connection() as conn:
            scope, boundary, _, pending = self._scope(conn, session_id)
            rows = conn.execute("SELECT * FROM companion_episodes WHERE owner=? AND status!='forgotten' ORDER BY created_at DESC,id LIMIT 200", (scope['avatar_id'],)).fetchall()
            return {'scope': scope, 'episodes': [self._item(conn, row) for row in rows],
                    'recall_after': boundary, 'repair_pending': pending,
                    'recall_budget': self._budget(conn, scope['avatar_id'], scope['dataset_epoch'])}

    def sources(self, session_id):
        """Bounded message choices for deliberate curation, without inference."""
        with self.connection() as conn:
            scope, boundary, _, pending = self._scope(conn, session_id)
            choices = []
            if not pending:
                rows = conn.execute("SELECT id,timestamp FROM messages WHERE session_id=? AND id>? AND role IN ('user','assistant') ORDER BY id DESC LIMIT 60",
                                    (session_id, boundary)).fetchall()
                for row in rows:
                    try:
                        source, excerpt, _, _, _ = _source(conn, scope['avatar_id'], {'message_id': row['id']}, session_id=session_id)
                    except EpisodeError:
                        continue
                    choices.append({key: source[key] for key in ('message_id', 'session_id', 'role')} |
                                   {'preview': excerpt[:120], 'timestamp': row['timestamp']})
            return {'scope': scope, 'sources': choices, 'recall_after': boundary, 'repair_pending': pending}

    def _insert(self, conn, scope, data, capture_key, *, curation, now=None, expected=False):
        existing = conn.execute('SELECT * FROM companion_episodes WHERE owner=? AND dataset_epoch=? AND capture_key=?', (scope['avatar_id'], scope['dataset_epoch'], capture_key)).fetchone()
        fingerprint = _hash(_json(data))
        if existing:
            if curation == 'reflection':
                return existing  # Stable original evidence preserves the first candidate, corrections and Forget.
            if existing['capture_fingerprint'] != fingerprint:
                raise EpisodeError('That request_id already belongs to a different episode capture.', 409)
            return existing
        _, boundary, revision, pending = self._scope(conn, scope['session_id'], scope['dataset_epoch'], write=True)
        if pending:
            raise EpisodeError('Repair this companion’s knowledge before capturing episodes.', 409)
        kind = data.get('kind')
        if not isinstance(kind, str) or kind not in KINDS:
            raise EpisodeError('kind must be actual_exchange, imagined_scene, or derived_summary.')
        title = _text(data.get('title'), 'title', 160)
        summary = _text(data.get('summary', ''), 'summary', 1200, empty=True)
        uncertainty = _text(data.get('uncertainty', ''), 'uncertainty', 400, empty=True)
        salience = _integer(data.get('salience', 3), 'salience', 1, 5)
        in_story = data.get('in_story', False)
        if type(in_story) is not bool:
            raise EpisodeError('in_story must be a boolean.')
        specs = data.get('sources')
        if not isinstance(specs, list) or not 1 <= len(specs) <= MAX_SOURCES:
            raise EpisodeError('Choose one or two source messages.')
        refs, total = [], 0
        for spec in specs:
            ref, excerpt, _, _, _ = _source(conn, scope['avatar_id'], spec, session_id=scope['session_id'], expected=expected)
            if ref['message_id'] in {r['message_id'] for r in refs}:
                raise EpisodeError('Each source message may appear only once.')
            refs.append(ref)
            total += len(excerpt)
        if total > MAX_SOURCE_CHARS:
            raise EpisodeError('Episode source excerpts exceed the character budget.')
        confidence = data.get('confidence') if curation == 'reflection' else None
        if confidence is not None and (type(confidence) not in {int, float} or not math.isfinite(confidence) or not 0 <= confidence <= 1):
            raise EpisodeError('Extraction confidence must be a finite number from zero to one.')
        identifier, stamp = 'ep_' + uuid.uuid4().hex, self.now(now).isoformat()
        conn.execute('''INSERT INTO companion_episodes(id,owner,session_id,dataset_epoch,kind,title,summary,
            uncertainty,confidence,salience,in_story,curation,source_refs,recall_floor,knowledge_revision,
            capture_key,capture_fingerprint,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                     (identifier, scope['avatar_id'], scope['session_id'], scope['dataset_epoch'], kind,
                      title, summary, uncertainty, confidence, salience, int(in_story), curation, _json(refs),
                      boundary, revision, capture_key, fingerprint, stamp, stamp))
        return conn.execute('SELECT * FROM companion_episodes WHERE id=?', (identifier,)).fetchone()

    def capture(self, session_id, epoch, data, *, owner=None):
        allowed = {'request_id', 'kind', 'title', 'summary', 'uncertainty', 'sources', 'salience', 'in_story'}
        if not isinstance(data, dict) or not set(data) <= allowed:
            raise EpisodeError('Unsupported episode capture fields.')
        request_id = data.get('request_id')
        if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,80}', request_id):
            raise EpisodeError('request_id must contain 8 to 80 letters, digits, underscores or hyphens.')
        with self.connection(write=True) as conn:
            scope, _, _, _ = self._scope(conn, session_id, epoch, write=True, expected_owner=owner)
            row = self._insert(conn, scope, data, 'manual:' + request_id,
                               curation='manual_curated' if data.get('summary') else 'manual_saved')
            return {'scope': scope, 'episode': self._item(conn, row)}

    def _owned(self, conn, episode_id, scope, *, historical=False):
        if not isinstance(episode_id, str) or not re.fullmatch(r'ep_[a-f0-9]{32}', episode_id):
            raise EpisodeError('Episode not found.', 404)
        row = conn.execute('SELECT * FROM companion_episodes WHERE id=? AND owner=?' + ('' if historical else ' AND dataset_epoch=?'),
                           (episode_id, scope['avatar_id']) if historical else (episode_id, scope['avatar_id'], scope['dataset_epoch'])).fetchone()
        if not row:
            raise EpisodeError('Episode not found for this companion.', 404)
        return row

    def _idle_for_correction(self, conn, owner):
        if conn.execute("SELECT 1 FROM chat_turns t JOIN sessions s ON s.id=t.session_id WHERE COALESCE(s.avatar_id,'')=? AND t.status IN ('sending','streaming')", (owner,)).fetchone():
            raise EpisodeError('Wait for this companion’s active reply to finish before correcting episodes.', 409)

    def change(self, episode_id, session_id, epoch, revision, changes, *, owner=None):
        allowed = {'title', 'summary', 'uncertainty', 'kind', 'salience', 'pinned', 'in_story'}
        if not isinstance(changes, dict) or not changes or not set(changes) <= allowed:
            raise EpisodeError('Choose supported episode correction fields.')
        with self.connection(write=True) as conn:
            scope, _, _, _ = self._scope(conn, session_id, epoch, write=True, expected_owner=owner)
            self._idle_for_correction(conn, scope['avatar_id'])
            row = self._owned(conn, episode_id, scope)
            if type(revision) is not int or revision != row['revision']:
                raise EpisodeError('Episode changed. Reload before saving your correction.', 409)
            if not self._item(conn, row)['status'] == 'current':
                raise EpisodeError('Capture current source evidence before correcting this episode.', 409)
            values = dict(row)
            for name in ('title', 'summary', 'uncertainty'):
                if name in changes:
                    values[name] = _text(changes[name], name, {'title': 160, 'summary': 1200, 'uncertainty': 400}[name], empty=name != 'title')
            if 'kind' in changes:
                if not isinstance(changes['kind'], str) or changes['kind'] not in KINDS:
                    raise EpisodeError('Unsupported episode kind.')
                values['kind'] = changes['kind']
            if 'salience' in changes:
                values['salience'] = _integer(changes['salience'], 'salience', 1, 5)
            for name in ('pinned', 'in_story'):
                if name in changes:
                    if type(changes[name]) is not bool:
                        raise EpisodeError(f'{name} must be a boolean.')
                    values[name] = int(changes[name])
            curation = row['curation']
            if set(changes) & {'title', 'summary', 'uncertainty', 'kind'}:
                curation = 'manual_corrected'
            elif changes.get('in_story') is True and curation == 'reflection':
                curation = 'manual_curated'
            conn.execute('''UPDATE companion_episodes SET revision=revision+1,title=?,summary=?,uncertainty=?,
                kind=?,salience=?,pinned=?,in_story=?,curation=?,updated_at=? WHERE id=?''',
                         tuple(values[k] for k in ('title', 'summary', 'uncertainty', 'kind', 'salience', 'pinned', 'in_story')) + (curation, self.now().isoformat(), episode_id))
            return {'scope': scope, 'episode': self._item(conn, self._owned(conn, episode_id, scope))}

    def _restore_review(self, conn, episode_id, scope, revision):
        row = self._owned(conn, episode_id, scope, historical=True)
        if row['status'] == 'forgotten' or row['dataset_epoch'] == scope['dataset_epoch']:
            raise EpisodeError('Only a retained episode from an older dataset can be reviewed.', 409)
        if type(revision) is not int or revision != row['revision']:
            raise EpisodeError('Episode changed. Reload before reviewing it.', 409)
        # Archives preserve curation, but imported fields are still untrusted.
        if row['kind'] not in KINDS or row['curation'] not in {'manual_saved', 'manual_curated', 'manual_corrected', 'reflection'}:
            raise EpisodeError('The retained episode has an unsupported source or curation label.', 422)
        _text(row['title'], 'title', 160)
        _text(row['summary'], 'summary', 1200, empty=True)
        _text(row['uncertainty'], 'uncertainty', 400, empty=True)
        _integer(row['salience'], 'salience', 1, 5)
        for name in ('pinned', 'in_story'):
            _integer(row[name], name, 0, 1)
        confidence = row['confidence']
        if confidence is not None and (type(confidence) not in {int, float} or not math.isfinite(confidence) or not 0 <= confidence <= 1):
            raise EpisodeError('The retained episode has invalid extraction confidence.', 422)
        refs, evidence = _refs(row['source_refs']), []
        if not refs:
            raise EpisodeError('No retained source evidence is available.', 410)
        for ref in refs:
            source, excerpt, _, boundary, knowledge_revision = _source(conn, scope['avatar_id'], ref, expected=True)
            if (boundary != row['recall_floor'] or knowledge_revision != row['knowledge_revision'] or
                    source['session_id'] != row['session_id']):
                raise EpisodeError('The original recall boundary or source context changed. Capture a new episode.', 409)
            evidence.append({**source, 'excerpt': excerpt, 'status': 'current'})
        return row, {'scope': scope, 'episode': {**dict(row), 'avatar_id': row['owner'], 'status': 'review_ready'},
                     'sources': evidence, 'reviewed_sources': refs}

    def review_restored(self, episode_id, session_id, epoch, revision, *, owner=None):
        """Explicit owner read; reveal retained curation only after sources match."""
        with self.connection() as conn:
            scope, _, _, _ = self._scope(conn, session_id, epoch, write=True, expected_owner=owner)
            _, result = self._restore_review(conn, episode_id, scope, revision)
            # Only expose the documented stored curation, never arbitrary DB fields.
            result['episode'] = {key: result['episode'][key] for key in
                                 ('id', 'avatar_id', 'revision', 'kind', 'title', 'summary', 'uncertainty', 'status')}
            return result

    def revalidate_restored(self, episode_id, session_id, epoch, revision, reviewed_sources, *, owner=None):
        with self.connection(write=True) as conn:
            scope, _, _, _ = self._scope(conn, session_id, epoch, write=True, expected_owner=owner)
            self._idle_for_correction(conn, scope['avatar_id'])
            row = self._owned(conn, episode_id, scope, historical=True)
            if (row['dataset_epoch'] == epoch and type(revision) is int and row['revision'] == revision + 1 and
                    self._item(conn, row)['status'] == 'current' and reviewed_sources == _refs(row['source_refs'])):
                return {'scope': scope, 'episode': self._item(conn, row)}
            _, checked = self._restore_review(conn, episode_id, scope, revision)
            if reviewed_sources != checked['reviewed_sources']:
                raise EpisodeError('Review the exact original source hashes and ranges before restoring curation.', 409)
            if conn.execute('SELECT 1 FROM companion_episodes WHERE owner=? AND dataset_epoch=? AND capture_key=? AND id!=?',
                            (scope['avatar_id'], epoch, row['capture_key'], episode_id)).fetchone():
                raise EpisodeError('This source capture already has an episode in the current dataset. Review that episode before restoring older curation.', 409)
            conn.execute("UPDATE companion_episodes SET dataset_epoch=?,status='current',reason='',revision=revision+1,updated_at=? WHERE id=?",
                         (epoch, self.now().isoformat(), episode_id))
            return {'scope': scope, 'episode': self._item(conn, self._owned(conn, episode_id, scope))}

    def forget(self, episode_id, session_id, epoch, revision, *, owner=None):
        # Validate before marking dirty, clearing notebooks or advancing recall.
        with self.connection() as conn:
            scope, _, _, _ = self._scope(conn, session_id, epoch, write=True, expected_owner=owner)
            row = self._owned(conn, episode_id, scope, historical=True)
            if row['status'] == 'forgotten':
                return {'scope': scope, 'episode': self._item(conn, row)}
            if type(revision) is not int or revision != row['revision']:
                raise EpisodeError('Episode changed. Reload before forgetting it.', 409)
        from core.knowledge import KnowledgeError, dirty
        knowledge = self._forget_knowledge()
        try:
            with knowledge.changing(scope['avatar_id']):
                # No writer connection crosses _boundary's own SQLite writes.
                with self._trusted_connection() as conn:
                    fresh, _, _, _ = self._scope(conn, session_id, epoch, write=True, expected_owner=scope['avatar_id'])
                    row = self._owned(conn, episode_id, fresh, historical=True)
                    if row['status'] == 'forgotten':
                        return {'scope': fresh, 'episode': self._item(conn, row)}
                    if row['revision'] != revision:
                        raise EpisodeError('Episode changed. Reload before forgetting it.', 409)
                prior_pending = dirty(knowledge.ds, scope['avatar_id'])
                knowledge._pending(scope['avatar_id'], True)
                knowledge._clear_notes(scope['avatar_id'])
                knowledge._boundary(scope['avatar_id'])
                with self._trusted_connection(write=True) as conn:
                    self._scope(conn, session_id, epoch, write=True, expected_owner=scope['avatar_id'])
                    row = self._owned(conn, episode_id, scope, historical=True)
                    if row['revision'] != revision:
                        raise EpisodeError('Episode changed while forgetting it. Reload and review.', 409)
                    self._tombstone(conn, episode_id)
                    result = {'scope': scope, 'episode': self._item(conn, self._owned(conn, episode_id, scope, historical=True)),
                              'recall_after': _key(conn, 'knowledge_recall:' + (scope['avatar_id'] or '@default')).get('through', 0),
                              'notice': 'Earlier conversation remains visible but is no longer recalled. Derived notes were cleared.'}
                knowledge._pending(scope['avatar_id'], prior_pending)
                return result
        except KnowledgeError as error:
            raise EpisodeError(str(error), error.status) from None

    def _forget_knowledge(self):
        if self.knowledge_factory is not None:
            return self.knowledge_factory(self.store)
        from core import datastore
        from core import notes as notebook
        from core.knowledge import KnowledgeService
        notes = self.notes_service
        current_notes = notebook._service
        if notes is None and current_notes is not None and current_notes._ds() is self.store:
            notes = current_notes
        if notes is None and self.store is datastore._datastore_instance:
            from core.notes import get_notes_service
            notes = get_notes_service()
        return KnowledgeService(self.store, manager=None, notes_service=notes)

    @contextmanager
    def _trusted_connection(self, *, write=False):
        """Only called while KnowledgeService.changing owns maintenance."""
        conn = self.store._connect()
        try:
            with conn:
                conn.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
                yield conn
        finally:
            conn.close()

    def _tombstone(self, conn, episode_id):
        conn.execute("UPDATE companion_episodes SET revision=revision+1,status='forgotten',title='',summary='',uncertainty='',source_refs='[]',pinned=0,in_story=0,reason='Forgotten by you.',updated_at=? WHERE id=?", (self.now().isoformat(), episode_id))

    def propose_reflection(self, conn, *, owner, session_id, epoch, run_id, proposals, now=None):
        """Publish bounded derived candidates in the reflection writer's transaction."""
        if not conn.in_transaction:
            raise EpisodeError('Reflection publication requires an active transaction.', 409)
        scope, _, _, _ = self._scope(conn, session_id, epoch, write=True)
        if scope['avatar_id'] != owner:
            raise EpisodeError('Reflection companion changed.', 409)
        if not isinstance(run_id, str) or not 1 <= len(run_id) <= 160:
            raise EpisodeError('Invalid reflection run identity.')
        if not isinstance(proposals, list) or len(proposals) > 2:
            raise EpisodeError('Reflection may propose at most two episodes.')
        result = []
        for proposal in proposals:
            if not isinstance(proposal, dict) or not set(proposal) <= {'title', 'summary', 'uncertainty', 'confidence', 'sources'}:
                raise EpisodeError('Unsupported reflection episode fields.')
            if not isinstance(proposal.get('sources'), list) or not proposal['sources'] or any(not isinstance(s, dict) or s.get('role') != 'user' for s in proposal['sources']):
                raise EpisodeError('Reflection episodes require original user evidence.')
            key = 'reflection:' + run_id + ':' + _hash(_json(proposal['sources']))
            row = self._insert(conn, scope, {**proposal, 'kind': 'derived_summary'}, key,
                               curation='reflection', now=now, expected=True)
            result.append(self._item(conn, row, now=now))
        return result

    def preview_recall(self, session_id, query, *, limit=2, char_budget=MAX_RECALL_CHARS):
        query = _text(query, 'query', 4000, empty=True)
        _integer(limit, 'limit', 1, 2)
        _integer(char_budget, 'char_budget', 1, MAX_RECALL_CHARS)
        with self.connection() as conn:
            scope, _, _, pending = self._scope(conn, session_id)
            budget = self._budget(conn, scope['avatar_id'], scope['dataset_epoch'])
            selected = self._select(conn, scope, query, limit, char_budget) if not pending and budget['available'] else []
            return {'scope': scope, 'episodes': selected, 'budget': budget,
                    'instructions': 'Use only attributed evidence. Imagined scenes and assistant guesses are never factual user biography. Ask when evidence is missing; never invent remember-when details.'}

    def _select(self, conn, scope, query, limit=2, char_budget=MAX_RECALL_CHARS, *, identifiers=None):
        if not query.strip() and identifiers is None:
            return []
        rows = ([self._owned(conn, identifier, scope) for identifier in identifiers] if identifiers is not None else
                conn.execute("SELECT * FROM companion_episodes WHERE owner=? AND dataset_epoch=? AND status='current' AND curation!='reflection' ORDER BY created_at DESC LIMIT 200", (scope['avatar_id'], scope['dataset_epoch'])).fetchall())
        items = [self._item(conn, row, query) for row in rows]
        items = [item for item in items if item['eligible'] and (identifiers is not None or item['ranking']['relevance'] > 0)]
        if identifiers is None:
            items.sort(key=lambda item: (-item['ranking']['score'], item['id']))
        selected, remaining = [], char_budget
        for item in items:
            if len(selected) >= limit:
                break
            source_text = '\n'.join(f"{s['role']} message {s['message_id']} [{s['start']}:{s['end']}]: {s['excerpt']}" for s in item['sources'])
            text = f"{item['label']}\nTitle: {item['title']}\nCurated summary (may be mistaken): {item['summary']}\nUncertainty: {item['uncertainty']}\nEvidence:\n{source_text}"
            if len(text) > remaining:
                continue
            selected.append({'id': item['id'], 'kind': item['kind'], 'text': text,
                             'sources': item['sources'], 'ranking': item['ranking']})
            remaining -= len(text)
        return selected

    def _reservation_snapshot(self, conn, scope, identifiers):
        snapshot = []
        for identifier in identifiers:
            row = self._owned(conn, identifier, scope)
            if not self._item(conn, row)['eligible']:
                raise EpisodeError('Reserved episode evidence changed.', 409)
            snapshot.append(dict(row))
        return _hash(_json(snapshot))

    def select_for_turn(self, session_id, epoch, request_id, query):
        """Reserve one companion slot for a real durable turn; spend nothing yet."""
        from core.turns import PROCESS_ID
        query = _text(query, 'query', 4000, empty=True)
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 160:
            raise EpisodeError('A durable request_id is required for episodic recall.')
        with self.connection(write=True) as conn:
            scope, _, _, pending = self._scope(conn, session_id, epoch, write=True)
            turn = conn.execute('SELECT * FROM chat_turns WHERE request_id=?', (request_id,)).fetchone()
            if (not turn or turn['session_id'] != session_id or turn['process_id'] != PROCESS_ID or
                    turn['status'] not in {'sending', 'streaming'} or pending):
                return []
            old = conn.execute('SELECT * FROM episode_recall_reservations WHERE owner=? AND dataset_epoch=?', (scope['avatar_id'], epoch)).fetchone()
            if old and old['request_id'] != request_id:
                return []
            if not self._budget(conn, scope['avatar_id'], epoch)['available']:
                return []
            if old:
                try:
                    old_ids = json.loads(old['episode_ids'])
                    if old['snapshot_sha256'] != self._reservation_snapshot(conn, scope, old_ids):
                        return []
                except (EpisodeError, TypeError, ValueError):
                    return []
                # Never change the evidence selected for a retrying durable turn.
                return self._select(conn, scope, query, identifiers=old_ids)
            selected = self._select(conn, scope, query)
            if selected:
                identifiers = [item['id'] for item in selected]
                conn.execute('''INSERT INTO episode_recall_reservations(request_id,owner,session_id,dataset_epoch,
                    process_id,episode_ids,snapshot_sha256) VALUES(?,?,?,?,?,?,?)''',
                             (request_id, scope['avatar_id'], session_id, epoch, PROCESS_ID, _json(identifiers),
                              self._reservation_snapshot(conn, scope, identifiers)))
            return selected

    def release_recall(self, request_id):
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 160:
            raise EpisodeError('Invalid durable request_id.')
        with self.connection(write=True) as conn:
            conn.execute('DELETE FROM episode_recall_reservations WHERE request_id=?', (request_id,))

    def record_recall(self, session_id, epoch, episode_ids, user_message_id, assistant_message_id, now=None, *, request_id=None):
        """Account only after a successfully saved exchange, including [] turns.

        A preview never reserves budget. A real turn's pending reservation protects
        its slot until successful publication or explicit release after failure.
        """
        with self.connection(write=True) as conn:
            return self._record_recall(conn, session_id, epoch, episode_ids, user_message_id, assistant_message_id,
                                       now, request_id=request_id)

    def _record_recall(self, conn, session_id, epoch, episode_ids, user_message_id, assistant_message_id, now=None, *, request_id=None):
        if not isinstance(episode_ids, list) or len(episode_ids) > 2 or any(not isinstance(i, str) for i in episode_ids) or len(set(episode_ids)) != len(episode_ids):
            raise EpisodeError('Provide at most two distinct episode IDs.')
        _integer(user_message_id, 'user_message_id')
        _integer(assistant_message_id, 'assistant_message_id')
        scope, boundary, _, pending = self._scope(conn, session_id, epoch, write=True)
        previous = conn.execute('SELECT * FROM episode_recall_turns WHERE user_message_id=? OR assistant_message_id=?', (user_message_id, assistant_message_id)).fetchone()
        if previous:
            return previous['owner'] == scope['avatar_id'] and json.loads(previous['episode_ids']) == episode_ids
        pair = conn.execute('SELECT id,role FROM messages WHERE session_id=? AND id>=? AND id<=? ORDER BY id', (session_id, user_message_id, assistant_message_id)).fetchall()
        if user_message_id <= boundary or [tuple(r) for r in pair] != [(user_message_id, 'user'), (assistant_message_id, 'assistant')]:
            return False
        turns = conn.execute('SELECT request_id,status FROM chat_turns WHERE user_message_id=? OR assistant_message_id=?', (user_message_id, assistant_message_id)).fetchall()
        if any(row['status'] != 'saved' or (request_id is not None and row['request_id'] != request_id) for row in turns):
            return False
        accepted = not pending and self._budget(conn, scope['avatar_id'], scope['dataset_epoch'], now)['available']
        reservation = None
        if episode_ids and request_id is not None:
            reservation = conn.execute('SELECT * FROM episode_recall_reservations WHERE request_id=? AND owner=? AND session_id=? AND dataset_epoch=?', (request_id, scope['avatar_id'], session_id, epoch)).fetchone()
            try:
                accepted = accepted and bool(reservation) and json.loads(reservation['episode_ids']) == episode_ids and reservation['snapshot_sha256'] == self._reservation_snapshot(conn, scope, episode_ids)
            except (EpisodeError, TypeError, ValueError):
                accepted = False
        elif episode_ids:
            # Legacy direct service accounting may not steal a durable turn's slot.
            accepted = accepted and not conn.execute('SELECT 1 FROM episode_recall_reservations WHERE owner=? AND dataset_epoch=?', (scope['avatar_id'], epoch)).fetchone()
        for identifier in episode_ids:
            try:
                accepted = accepted and self._item(conn, self._owned(conn, identifier, scope))['eligible']
            except EpisodeError:
                accepted = False
        actual = episode_ids if accepted else []
        conn.execute('INSERT INTO episode_recall_turns(user_message_id,assistant_message_id,owner,dataset_epoch,episode_ids,used_at) VALUES(?,?,?,?,?,?)', (user_message_id, assistant_message_id, scope['avatar_id'], scope['dataset_epoch'], _json(actual), self.now(now).isoformat()))
        if reservation is not None:
            conn.execute('DELETE FROM episode_recall_reservations WHERE request_id=?', (request_id,))
        return accepted or not episode_ids


def get_episode_service(store=None):
    if store is None:
        from core.datastore import get_datastore
        store = get_datastore()
    service = getattr(store, '_episode_service', None)
    if service is None:
        service = store._episode_service = EpisodeService(store)
    return service
