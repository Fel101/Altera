"""Local per-reply measurements; no prompts, credentials or telemetry exports."""
import ipaddress
import json
import math
from contextlib import closing
from urllib.parse import urlsplit

from core.turns import CURRENT_TURN


def network_provider(base_url):
    host = urlsplit(base_url or '').hostname or ''
    if host.lower() == 'localhost':
        return False
    try:
        return not ipaddress.ip_address(host).is_loopback
    except ValueError:
        return True


def begin_call(client, messages, tools=None):
    turn = CURRENT_TURN.get()
    if not turn:
        return None
    record = {'provider': getattr(client, 'provider_key', None) or 'custom', 'model': str(client.model),
              'network_provider': network_provider(client.base_url),
              'cost_unit': 'OpenRouter credits' if urlsplit(client.base_url).hostname == 'openrouter.ai' else None,
              'input_tokens_estimate': math.ceil(len(json.dumps({'messages': messages, 'tools': tools}, ensure_ascii=False)) / 3.2)}
    turn.metrics.setdefault('calls', []).append(record)
    return record


def record_usage(record, usage):
    if record is None or usage is None:
        return
    def field(name):
        return usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
    for name in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
        value = field(name)
        if type(value) is int and 0 <= value <= 2 ** 53 - 1:
            record[name] = value
    value = field('cost')
    # Only OpenRouter documents this field's units. A custom server's number has unknown units.
    if record.get('cost_unit') == 'OpenRouter credits' and type(value) in (int, float) and 0 <= value <= 2 ** 53 - 1 and math.isfinite(value):
        record['reported_cost_credits'] = value


def summarize(metrics):
    # Restored databases and older versions can contain malformed JSON. Return only documented measurements.
    def numeric(value):
        # Bound before isfinite: converting an arbitrarily large restored JSON integer to float can overflow.
        return type(value) in (int, float) and 0 <= value <= 2 ** 53 - 1 and math.isfinite(value)
    metrics = metrics if isinstance(metrics, dict) else {}
    raw = metrics.get('calls')
    calls = []
    for call in (raw[:64] if isinstance(raw, list) else []):
        if not isinstance(call, dict):
            continue
        clean = {name: call[name] for name in ('prompt_tokens', 'completion_tokens', 'total_tokens', 'reported_cost_credits', 'input_tokens_estimate') if numeric(call.get(name))}
        clean.update({name: str(call.get(name) or '')[:160] for name in ('provider', 'model')})
        calls.append(clean)
    result = {'calls': calls}
    if numeric(metrics.get('visible_output_tokens_estimate')):
        result['visible_output_tokens_estimate'] = metrics['visible_output_tokens_estimate']
    context = metrics.get('context')
    fields = ('limit_tokens', 'system_tokens_estimate', 'history_tokens_estimate', 'reply_reserve_estimate', 'max_reply_tokens', 'history_messages', 'history_candidates', 'history_after')
    if isinstance(context, dict) and all(numeric(context.get(key)) for key in fields):
        result['context'] = {key: context[key] for key in fields}
        result['context']['limit_source'] = str(context.get('limit_source') or 'Unknown')[:80]
    if metrics.get('notes_action') in ('compact', 'pass'):
        result['notes_action'] = metrics['notes_action']
    result['call_count'] = len(calls)
    for field in ('prompt_tokens', 'completion_tokens', 'total_tokens', 'reported_cost_credits'):
        values = [c[field] for c in calls if field in c]
        result[field] = {'value': sum(values) if values else None, 'reported_calls': len(values), 'total_calls': len(calls)}
    result['input_tokens_estimate'] = sum(c.get('input_tokens_estimate', 0) for c in calls) if calls else None
    return result


def snapshot(store, session_id):
    from core.config import get_config
    from core.providers import get_provider_registry
    from core import notes
    from core.knowledge import dirty, floor
    registry = get_provider_registry(get_config())
    provider = registry.get_active_provider()
    session = store.get_session(session_id)
    if not session:
        return None
    with closing(store._connect()) as conn:
        rows = conn.execute('SELECT request_id,status,first_text_ms,elapsed_ms,created_at,diagnostics FROM chat_turns WHERE session_id=? ORDER BY created_at DESC,rowid DESC LIMIT 20', (session_id,)).fetchall()
    turns = []
    for row in rows:
        item = dict(row)
        try:
            metrics = json.loads(item.pop('diagnostics') or '{}')
            if not isinstance(metrics, dict):
                metrics = {}
        except (TypeError, ValueError):
            metrics = {}
        item['metrics'] = summarize(metrics)
        turns.append(item)
    owner = session.avatar_id or ''
    pending = dirty(store, owner)
    notebook = {'available': bool(owner), 'paused': bool(pending), 'recall_after': floor(store, owner)}
    if owner:
        service = notes.get_notes_service()
        status = service.status(owner, session_id)
        # No note text or exception text in this endpoint.
        notebook.update({key: status[key] for key in ('busy', 'updated_at', 'failures', 'words', 'waiting', 'compacted_through')})
    return {'active': {'provider': registry._active_provider_key, 'name': provider.name if provider else '',
                       'model': registry.get_active_model(), 'network_provider': network_provider(provider.base_url) if provider else None},
            'session_id': session_id, 'turns': turns, 'notes': notebook,
            'scope': 'Latest 20 replies in this conversation. Includes synchronous capability/tool calls; excludes background notes, memory extraction, images and speech. Provider invoices remain authoritative.'}
