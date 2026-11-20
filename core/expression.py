"""Small, simulated companion expression, derived only from a completed exchange.

This is a presentation cue, never evidence about the user or an emotional need.
Reads decay it without a timer. Source edits/Forget/restore fail back to calm.
"""
from datetime import datetime, UTC
import hashlib
import json
import math
import re


HALF_LIFE_SECONDS = 6 * 3600
FRAMES = {'calm': None, 'warm': 'happy', 'happy': 'happy', 'excited': 'surprised',
          'curious': 'thinking', 'concerned': 'sad'}
RULES = (
    ('concerned', r"\b(?:i'm|i am) (?:sorry|concerned)|\bthat sounds (?:hard|painful)\b"),
    ('excited', r"\b(?:i'm|i am) (?:so )?excited\b|\bthat's (?:wonderful|amazing)\b"),
    ('happy', r"\b(?:i'm|i am) (?:so )?(?:happy|glad)\b|😊|😄"),
    ('curious', r"\b(?:i'm|i am) curious\b|\bi wonder\b"),
    ('warm', r"\bthank you\b|\bi appreciate\b|\bi'm here with you\b"),
)


def ensure_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS companion_expression (
        owner TEXT PRIMARY KEY, session_id INTEGER NOT NULL, dataset_epoch TEXT NOT NULL,
        state TEXT NOT NULL, intensity REAL NOT NULL, updated_at TEXT NOT NULL,
        user_id INTEGER NOT NULL, assistant_id INTEGER NOT NULL,
        user_sha256 TEXT NOT NULL, assistant_sha256 TEXT NOT NULL,
        recall_floor INTEGER NOT NULL, knowledge_revision INTEGER NOT NULL)''')


def invalidate_owner(conn, owner, reason=None):
    conn.execute('DELETE FROM companion_expression WHERE owner=?', (owner or '',))
    conn.execute("UPDATE chat_turns SET expression_pending=NULL,memory_pending=NULL WHERE session_id IN (SELECT id FROM sessions WHERE COALESCE(avatar_id,'')=?)", (owner or '',))


def invalidate_session(conn, session_id, reason=None):
    conn.execute('DELETE FROM companion_expression WHERE session_id=?', (session_id,))
    conn.execute("UPDATE chat_turns SET expression_pending=NULL,memory_pending=NULL WHERE session_id IN (SELECT id FROM sessions WHERE COALESCE(avatar_id,'')=(SELECT COALESCE(avatar_id,'') FROM sessions WHERE id=?))", (session_id,))


def _now(value):
    value = value if value is not None else datetime.now(UTC)
    if value.tzinfo is None:
        raise ValueError('Expression clock must include a timezone.')
    return value.astimezone(UTC)


def _key(conn, key):
    row = conn.execute('SELECT value FROM user_profile WHERE key=?', (key,)).fetchone()
    value = json.loads(row[0]) if row else {}
    if not isinstance(value, dict):
        raise ValueError('Invalid expression context.')
    return value


def _context(conn, owner, session_id):
    row = conn.execute("SELECT COALESCE(avatar_id,'') FROM sessions WHERE id=?", (session_id,)).fetchone()
    if row is None or row[0] != owner:
        raise ValueError('Expression conversation changed.')
    boundary = _key(conn, 'knowledge_recall:' + (owner or '@default')).get('through') or 0
    dirty = _key(conn, f'knowledge_dirty:{session_id}')
    owner_dirty = _key(conn, 'knowledge_dirty_owner:' + (owner or '@default'))
    # An edit elsewhere can invalidate a summary that influenced this exchange.
    for item in conn.execute("SELECT p.value FROM user_profile p JOIN sessions s "
                             "ON p.key='knowledge_dirty:' || s.id WHERE COALESCE(s.avatar_id,'')=?", (owner,)):
        if json.loads(item[0]).get('pending'):
            raise ValueError('Companion knowledge needs repair.')
    if dirty.get('pending') or owner_dirty.get('pending'):
        raise ValueError('Companion knowledge needs repair.')
    epoch = _key(conn, 'dataset_epoch').get('value')
    if not isinstance(epoch, str) or not epoch:
        raise ValueError('Dataset identity is unavailable.')
    return epoch, int(boundary), int(dirty.get('revision') or 0)


def _source(conn, message_id, session_id, role, through):
    row = conn.execute('SELECT content FROM messages WHERE id=? AND session_id=? AND role=?',
                       (message_id, session_id, role)).fetchone()
    if not row or message_id <= through:
        raise ValueError('Expression source is unavailable.')
    return row[0], hashlib.sha256(row[0].encode('utf-8')).hexdigest()


def capture_pending(conn, session_id, user_id, assistant_id):
    """Capture source identity in the same transaction as the saved reply."""
    try:
        session = conn.execute("SELECT COALESCE(avatar_id,'') FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not session or type(user_id) is not int or type(assistant_id) is not int or user_id >= assistant_id:
            return None
        owner = session[0]
        epoch, through, revision = _context(conn, owner, session_id)
        _, user_sha = _source(conn, user_id, session_id, 'user', through)
        _, assistant_sha = _source(conn, assistant_id, session_id, 'assistant', through)
        if conn.execute('SELECT 1 FROM messages WHERE session_id=? AND id>? AND id<?',
                        (session_id, user_id, assistant_id)).fetchone():
            return None
        return json.dumps({'owner': owner, 'epoch': epoch, 'floor': through, 'revision': revision,
                           'user_sha256': user_sha, 'assistant_sha256': assistant_sha,
                           'saved_at': datetime.now(UTC).isoformat()}, separators=(',', ':'))
    except (ValueError, TypeError, KeyError, OverflowError, AttributeError):
        return None


def validate_pending(conn, session_id, user_id, assistant_id, expected):
    """Recheck an original source snapshot without changing expression state."""
    if not isinstance(expected, dict) or set(expected) != {'owner', 'epoch', 'floor', 'revision', 'user_sha256', 'assistant_sha256', 'saved_at'}:
        raise ValueError()
    moment = _now(datetime.fromisoformat(expected['saved_at']))
    if moment > _now(None):
        raise ValueError()
    session = conn.execute("SELECT COALESCE(avatar_id,'') FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not session:
        raise ValueError()
    owner = session[0]
    epoch, through, revision = _context(conn, owner, session_id)
    _, user_sha = _source(conn, user_id, session_id, 'user', through)
    _, assistant_sha = _source(conn, assistant_id, session_id, 'assistant', through)
    if ((expected['owner'], expected['epoch'], expected['floor'], expected['revision'],
         expected['user_sha256'], expected['assistant_sha256']) != (owner, epoch, through, revision, user_sha, assistant_sha)
            or type(user_id) is not int or type(assistant_id) is not int or user_id >= assistant_id
            or conn.execute('SELECT 1 FROM messages WHERE session_id=? AND id>? AND id<?',
                            (session_id, user_id, assistant_id)).fetchone()):
        raise ValueError()
    return moment


def _calm():
    return {'state': 'calm', 'intensity': 0.0, 'frame': None, 'simulated': True,
            'updated_at': None, 'half_life_hours': 6}


def get_expression(store, owner, now=None):
    """Read only; no inference, writes, retained conversation text, or absence penalty."""
    moment = _now(now)
    conn = store._connect()
    try:
        conn.execute('BEGIN')
        row = conn.execute('SELECT * FROM companion_expression WHERE owner=?', (owner or '',)).fetchone()
        if not row:
            return _calm()
        epoch, through, revision = _context(conn, owner or '', row['session_id'])
        if (epoch != row['dataset_epoch'] or through != row['recall_floor'] or
                revision != row['knowledge_revision'] or row['state'] not in FRAMES):
            return _calm()
        for role in ('user', 'assistant'):
            _, sha = _source(conn, row[role + '_id'], row['session_id'], role, through)
            if sha != row[role + '_sha256']:
                return _calm()
        updated = datetime.fromisoformat(row['updated_at'])
        if updated.tzinfo is None or updated > moment:
            return _calm()
        intensity = float(row['intensity'])
        if not math.isfinite(intensity) or not 0 <= intensity <= 1:
            return _calm()
        intensity *= 2 ** (-(moment - updated).total_seconds() / HALF_LIFE_SECONDS)
        if intensity < .08 or row['state'] == 'calm':
            return _calm()
        return {'state': row['state'], 'intensity': round(intensity, 4),
                'frame': FRAMES[row['state']] if intensity >= .2 else None,
                'simulated': True, 'updated_at': row['updated_at'], 'half_life_hours': 6}
    except (ValueError, TypeError, KeyError, OverflowError, AttributeError):
        return _calm()
    finally:
        conn.close()


def _record_exchange(conn, session_id, user_message_id, assistant_message_id, moment, expected=None):
    session = conn.execute("SELECT COALESCE(avatar_id,'') FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not session:
        return False
    owner = session[0]
    epoch, through, revision = _context(conn, owner, session_id)
    _, user_sha = _source(conn, user_message_id, session_id, 'user', through)
    content, assistant_sha = _source(conn, assistant_message_id, session_id, 'assistant', through)
    if expected is not None and (expected['owner'], expected['epoch'], expected['floor'], expected['revision'],
                                 expected['user_sha256'], expected['assistant_sha256']) != (
                                     owner, epoch, through, revision, user_sha, assistant_sha):
        return False
    # Reject replayed/older exchanges, non-adjacent sources and truncated drafts.
    if user_message_id >= assistant_message_id or conn.execute(
            'SELECT 1 FROM messages WHERE session_id=? AND id>? AND id<?',
            (session_id, user_message_id, assistant_message_id)).fetchone():
        return False
    old = conn.execute('SELECT assistant_id FROM companion_expression WHERE owner=?', (owner,)).fetchone()
    if old and old[0] >= assistant_message_id:
        return False
    state = next((name for name, pattern in RULES if re.search(pattern, content[:16000], re.I)), 'calm')
    conn.execute('''INSERT OR REPLACE INTO companion_expression
        (owner,session_id,dataset_epoch,state,intensity,updated_at,user_id,assistant_id,
         user_sha256,assistant_sha256,recall_floor,knowledge_revision)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                 (owner, session_id, epoch, state, .6 if state != 'calm' else 0., moment.isoformat(),
                  user_message_id, assistant_message_id, user_sha, assistant_sha, through, revision))
    return True


def record_exchange(store, session_id, user_message_id, assistant_message_id, now=None):
    """Idempotent cue from the assistant's wording in a saved, eligible exchange."""
    moment = _now(now)
    store.dataset_epoch()
    conn = store._connect()
    try:
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            return _record_exchange(conn, session_id, user_message_id, assistant_message_id, moment)
    except (ValueError, TypeError, KeyError, OverflowError, AttributeError):
        return False
    finally:
        conn.close()


def consume_pending(store, request_id):
    """Consume one saved, source-bound intent atomically; return whether it existed."""
    conn = store._connect()
    try:
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            turn = conn.execute('SELECT * FROM chat_turns WHERE request_id=?', (request_id,)).fetchone()
            if not turn or turn['expression_pending'] is None:
                return False
            if turn['status'] != 'saved':
                return True  # Never fall back to unbound data while an intent exists.
            try:
                raw = turn['expression_pending']
                if not isinstance(raw, str) or len(raw) > 2048:
                    raise ValueError()
                expected = json.loads(raw)
                moment = validate_pending(conn, turn['session_id'], turn['user_message_id'], turn['assistant_message_id'], expected)
                _record_exchange(conn, turn['session_id'], turn['user_message_id'], turn['assistant_message_id'], moment, expected)
            except (ValueError, TypeError, KeyError, OverflowError, AttributeError):
                pass  # Refuse stale, malformed or changed source; do not use fallback.
            conn.execute('UPDATE chat_turns SET expression_pending=NULL WHERE request_id=?', (request_id,))
            return True
    finally:
        conn.close()


def prompt_section(expression):
    state = (expression or {}).get('state')
    if state not in FRAMES or state == 'calm':
        return ''
    return ('\n## Simulated expression\nYour current presentation cue is ' + state +
            '. Let it lightly colour your tone only when appropriate to this message. '
            'This is a simulated expression, separate from the person\'s feelings; it is not a fact, '
            'a memory, or a reason to claim needs, resentment, or hurt about their absence. '
            'Do not mention a cause or shared event unless eligible conversation evidence supports it.')
