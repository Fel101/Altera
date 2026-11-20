"""Source-bound, retry-safe recency accounting for saved chat replies.

SQLite owns the intent; Chroma owns per-memory idempotency markers. This is
not a transaction across the two stores and never invokes an embedding model.
"""
import json
import logging
import re

LOGGER = logging.getLogger(__name__)


def _valid_capture(captured, context):
    if (not isinstance(captured, dict) or set(captured) != {'avatar_id', 'dataset_epoch', 'recall_after', 'items'}
            or not isinstance(context, dict) or captured['avatar_id'] != context.get('owner')
            or captured['dataset_epoch'] != context.get('epoch') or captured['recall_after'] != context.get('floor')
            or not isinstance(captured['items'], list) or not 0 < len(captured['items']) <= 20):
        return False
    seen = set()
    for item in captured['items']:
        if (not isinstance(item, dict) or set(item) != {'id', 'sha256'} or not isinstance(item['id'], str)
                or not 0 < len(item['id']) <= 160 or any(ord(char) < 32 for char in item['id'])
                or not isinstance(item['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', item['sha256'])
                or item['id'] in seen):
            return False
        seen.add(item['id'])
    return True


def encode_intent(captured, expression_pending):
    if not captured or not expression_pending:
        return None
    try:
        context = json.loads(expression_pending)
        if not _valid_capture(captured, context):
            return None
        value = json.dumps({'capture': captured, 'context': context}, ensure_ascii=True, separators=(',', ':'), allow_nan=False)
        return value if len(value) <= 8192 else None
    except (TypeError, ValueError, OverflowError):
        return None


def consume_pending(service, request_id, manager):
    """Recheck original context, update vectors, then conditionally clear intent.

    The caller owns an application-state lease. The turn lock serializes Altera's
    message/knowledge edits with revalidation and the vector metadata writes.
    Any write failure leaves the intent; per-memory reply IDs prevent replay
    from incrementing an already-completed update twice.
    """
    from core.datastore import get_datastore
    from core.expression import validate_pending
    from core.turns import get_turn_service
    store = service.store
    if store is not get_datastore() or service is not get_turn_service(store):
        raise ValueError('Memory recovery requires the current owned store and reply service.')
    with service.lock:
        with service.connection() as conn:
            row = conn.execute('SELECT * FROM chat_turns WHERE request_id=?', (request_id,)).fetchone()
            if not row or row['memory_pending'] is None:
                return False
            if row['status'] != 'saved':
                return True
            raw = row['memory_pending']
            try:
                if not isinstance(raw, str) or len(raw) > 8192:
                    raise ValueError()
                intent = json.loads(raw)
                if not isinstance(intent, dict) or set(intent) != {'capture', 'context'} or not _valid_capture(intent['capture'], intent['context']):
                    raise ValueError()
                moment = validate_pending(conn, row['session_id'], row['user_message_id'], row['assistant_message_id'], intent['context'])
            except (ValueError, TypeError, KeyError, OverflowError, AttributeError):
                conn.execute('UPDATE chat_turns SET memory_pending=NULL WHERE request_id=? AND memory_pending=?', (request_id, raw))
                return True
        manager.record_recalled(store, intent['capture'], row['user_message_id'], row['assistant_message_id'], now=moment)
        with service.connection() as conn:
            conn.execute('UPDATE chat_turns SET memory_pending=NULL WHERE request_id=? AND memory_pending=? AND status=?',
                         (request_id, raw, 'saved'))
        return True


def recover_pending(service, *, limit=1000):
    """Recover bounded previous-process intents on ordinary startup only."""
    from core.state_gate import STATE
    from core.turns import PROCESS_ID
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError('A bounded recovery limit is required.')
    lease = STATE.enter('memory-recovery')
    try:
        with service.connection() as conn:
            conn.execute("UPDATE chat_turns SET memory_pending=NULL WHERE process_id != ? AND status != 'saved' AND memory_pending IS NOT NULL", (PROCESS_ID,))
            ids = [row['request_id'] for row in conn.execute(
                "SELECT request_id FROM chat_turns WHERE process_id != ? AND status='saved' AND memory_pending IS NOT NULL ORDER BY assistant_message_id LIMIT ?", (PROCESS_ID, limit))]
        if ids:
            from core.memory import get_memory_manager
            manager = get_memory_manager()
            for request_id in ids:
                try:
                    consume_pending(service, request_id, manager)
                except Exception:
                    LOGGER.warning('A saved memory-recency update remains pending for retry.')
                    break  # Later reply IDs must not overtake an uncertain earlier update.
    finally:
        lease.release()
