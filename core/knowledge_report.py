"""Deterministic, scoped inspection of saved personal context, without a model.

Eligibility describes inputs the application permits, not what a model knows or
what was selected for a particular reply. Stored text is never summarized here.
"""
from datetime import datetime, UTC
import json
import math

from core import safety
from core.knowledge import dirty, floor, reference, references, source_status
from core.notes import settings


LIMITATIONS = [
    'This is a saved-context report, not a claim about the model’s internal knowledge or the truth of every statement.',
    'Eligible means the application permits this input. Semantic search and conversation/token limits decide what a reply receives; this report does not record a sent prompt.',
    'A current source confirms that the saved message still matches. It does not prove that an extracted statement is true. Assistant messages are not user assertions.',
    'Saved confidence values are extraction or user-supplied scores, not measured probabilities that a fact is true.',
    'Pins, salience and recency affect retrieval order independently of confidence. They neither confirm claims nor silently delete information. Legacy timestamps without an offset do not imply a known age.',
    'Notebook text is a derived aggregate. Its recorded range describes the latest update, not a source citation for each statement or a complete lineage.',
    'Reflection notebook entries are separate derived interpretations with exact original user-message ranges. They retain uncertainty and are not independent confirmations of earlier generated content.',
    'Profile name, hobbies, preferences and facts, global events, relationships, opinions and communication style are not added to normal companion chat by those stores and are omitted here. Persona, tools and photo descriptions are not a personal-fact inventory.',
    'Conversation ownership here is the saved owner. Unassigned legacy conversations appear under Default; opening one can use the active companion’s persona until legacy migration assigns it.',
]

REASONS = {
    'repair_pending': 'Conversation changes paused saved memories and notes until repair.',
    'source_changed': 'At least one source message has changed.',
    'source_unavailable': 'At least one source is missing, invalid or outside this companion’s conversations.',
    'source_unrecorded': 'This older memory has no recorded source and has not been explicitly confirmed by you.',
    'content_blocked': 'The chat safety filter excludes this saved memory.',
    'empty': 'No text is saved.',
    'legacy_recall_boundary': 'An explicit default-companion recall boundary suppresses this legacy context.',
}


def _eligibility(reasons):
    return {'eligible': not reasons, 'reason_codes': reasons}


def _scalar(value):
    """Only allow documented scalar fields, never arbitrary saved metadata."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    return None


def _owned_sources(memory, datastore, owner):
    """Source status is shared with recall; excerpts require a valid owned session."""
    result = []
    for source, recorded in zip(source_status(memory, datastore), references(memory.get('metadata'))):
        message = datastore.get_message(source.get('message_id')) if source.get('message_id') else None
        session = datastore.get_session(message.session_id) if message else None
        if not session or (session.avatar_id or '') != owner or source.get('session_id') != message.session_id:
            result.append({'status': 'unavailable'})
            continue
        result.append({key: source[key] for key in ('message_id', 'session_id', 'role', 'content', 'status') if key in source})
        result[-1]['sha256'] = reference(message)['sha256']
        result[-1]['recorded_sha256'] = recorded['sha256']
    return result


def _memory_item(memory, datastore, owner, pending, now):
    from core.salience import describe
    meta = memory.get('metadata') or {}
    sources = _owned_sources(memory, datastore, owner)
    origin = {'manual': 'user_added', 'manual_corrected': 'user_corrected',
              'auto_extracted': 'auto_extracted'}.get(meta.get('user_context'), 'legacy_unattributed')
    reasons = ['repair_pending'] if pending else []
    if any(s['status'] == 'changed' for s in sources):
        reasons.append('source_changed')
    if any(s['status'] == 'unavailable' for s in sources):
        reasons.append('source_unavailable')
    if not sources and origin not in {'user_added', 'user_corrected'}:
        reasons.append('source_unrecorded')
    if safety.blocks_message(memory.get('content') or ''):
        reasons.append('content_blocked')
    return {'id': memory['id'], 'content': memory.get('content') or '', 'type': _scalar(meta.get('type')) or 'fact',
            'origin': origin, 'confidence': _scalar(meta.get('confidence')),
            'extracted_by': _scalar(meta.get('extracted_by')) or '',
            'created_at': _scalar(meta.get('created_at')), 'updated_at': _scalar(meta.get('updated_at')),
            'eligibility': _eligibility(reasons), 'retrieval': 'semantic_search_and_priority',
            'priority': describe(meta, now), 'sources': sources}


def _notebook(service, owner, pending):
    if not owner or service.notes is None:
        return []
    store, ds = service.notes.store, service.ds
    state, text = store.state(owner), store.read(owner)
    provenance = {'kind': 'derived_aggregate', 'claim_sources_available': False,
                  'source_kind': state.get('source_kind') if state.get('source_kind') in {'conversation', 'user-edited'} else 'unrecorded',
                  'includes_user_edits': bool(state.get('includes_user_edits') or state.get('source_kind') == 'user-edited'),
                  'includes_legacy_notes': bool(state.get('includes_legacy_notes') or (not state.get('source_kind') and any(text.values())))}
    sid = state.get('source_session_id')
    session = ds.get_session(sid) if type(sid) is int else None
    if session and (session.avatar_id or '') == owner:
        provenance['latest_update_range'] = {'session_id': sid,
            'after': _scalar(state.get('source_after')), 'through': _scalar(state.get('source_through'))}
    if state.get('new_evidence_roles') == ['user']:
        provenance['new_evidence_roles'] = ['user']
    return [{'id': key, 'content': text.get(key) or '', 'origin': 'derived_note', 'retrieval': 'every_turn',
             'eligibility': _eligibility((['repair_pending'] if pending else []) + ([] if text.get(key) else ['empty'])),
             'provenance': dict(provenance)} for key in ('about_you', 'our_story')]


def _conversations(service, owner, boundary):
    """Bounded candidates, before token budgeting, photo expansion or regeneration."""
    from core.knowledge import prompt_messages
    cfg = settings(service.notes.config if service.notes else {})
    state = service.notes.store.state(owner) if owner and service.notes else {}
    compacted = int(state.get('compacted_through') or 0)
    result = []
    for session in sorted(service.ds.list_sessions(archived=None), key=lambda s: s.id):
        if (session.avatar_id or '') != owner:
            continue
        runtime_after = 0
        if owner and service.notes and not (service.ds.get_key(f'knowledge_dirty:{session.id}') or {}).get('pending'):
            # Use the runtime policy, including any session-specific compaction rules.
            runtime_after = service.notes.history_after(owner, session.id)
        after = max(boundary, runtime_after)
        records = (service.ds.get_messages_after(session.id, runtime_after) if runtime_after else
                   service.ds.get_messages(session.id, limit=cfg['history_messages']))
        messages = prompt_messages(service.ds, session.id, records, snapshot=True)[-cfg['history_messages']:]
        candidates = [{'message_id': m.id, 'session_id': session.id, 'role': m.role,
                       'content': m.content, 'sha256': reference(m)['sha256']} for m in messages]
        mood_records = prompt_messages(service.ds, session.id, service.ds.get_messages(session.id, limit=20), snapshot=True)
        moods = [m for m in mood_records if m.role == 'user' and isinstance(m.mood, dict) and m.mood.get('primary_emotion')]
        # EmotionSection uses only the most recent label, after computing its last-five window.
        mood = ({'content': str(moods[-1].mood['primary_emotion']), 'origin': 'inferred_mood',
                 'source': {**reference(moods[-1]), 'role': 'user', 'content': moods[-1].content}, 'retrieval': 'current_session'}
                if owner and moods else None)
        result.append({'session_id': session.id, 'archived': session.archived, 'history_after': after,
                       'recall_after': boundary, 'compacted_through': compacted if owner else 0,
                       'candidate_limit': cfg['history_messages'], 'history_candidates': candidates, 'mood_context': mood})
    return result


def _legacy_context(datastore, owner, boundary):
    if owner:
        return []
    # Only fields consumed by normal default-persona chat. Never export the profile/settings object.
    from core.profile import load_user_profile
    from core.jokes import JokeTracker
    moods = load_user_profile().get('mood_history') or []
    tracker = JokeTracker.__new__(JokeTracker)
    tracker.ds = datastore
    callbacks = tracker.get_callback_material(limit=2)
    humor = datastore.get_humor_style() or {}
    values = [('mood', str(moods[-1]), 'inferred_mood')] if isinstance(moods, list) and moods else []
    values.extend((f'callback-{i + 1}', c['text'], 'legacy_derived') for i, c in enumerate(callbacks))
    if 'style' in humor:
        values.append(('humor', f"Style: {humor.get('style', 'playful')}. Level: {humor.get('level', 'medium')}.", 'legacy_derived'))
    return [{'id': key, 'content': text, 'origin': origin, 'retrieval': 'default_persona',
             'eligibility': _eligibility(['legacy_recall_boundary'] if boundary else []),
             'provenance': {'kind': 'legacy_shared', 'claim_sources_available': False}}
            for key, text, origin in values]


def build_report(service, owner, name, *, generated_at=None):
    """Caller holds the application snapshot gate. All reads are scoped/whitelisted."""
    boundary, pending = floor(service.ds, owner), dirty(service.ds, owner)
    moment = datetime.fromisoformat(generated_at) if generated_at else datetime.now(UTC)
    memories = [_memory_item(m, service.ds, owner, pending, moment)
                for m in sorted(service._memories(owner), key=lambda m: str(m['id']))]
    notebook = _notebook(service, owner, pending)
    from core.reflection import current_notebook
    reflected = current_notebook(service.ds, owner, snapshot=True)
    reflected_items = []
    for entry in reflected['entries']:
        sources = []
        for ref in entry['sources']:
            source = service.ds.get_message(ref['message_id'])
            session = service.ds.get_session(source.session_id) if source else None
            if (source and session and (session.avatar_id or '') == owner and source.role == 'user' and
                    source.session_id == ref['session_id'] and reference(source)['sha256'] == ref['sha256']):
                sources.append({**ref, 'recorded_sha256': ref['sha256'], 'content': source.content[ref['start']:ref['end']]})
            else:
                sources.append({'status': 'unavailable'})
        reflected_items.append({'id': entry['id'], 'origin': 'derived_reflection', 'content': entry['text'],
            'section': entry['note'], 'confidence': entry['confidence'], 'user_edited': entry['user_edited'],
            'uncertainty': entry['uncertainty'], 'created_at': entry['created_at'], 'updated_at': entry['updated_at'],
            'eligibility': _eligibility(['source_unavailable'] if any(s['status'] == 'unavailable' for s in sources) else []),
            'retrieval': 'source_checked_reflection', 'sources': sources})
    conversations = _conversations(service, owner, boundary)
    legacy = _legacy_context(service.ds, owner, boundary)
    return {'schema_version': 1, 'generated_at': generated_at or datetime.now(UTC).isoformat(timespec='seconds'),
            'scope': {'id': owner or '_default', 'name': name},
            'recall': {'after': boundary, 'repair_pending': pending},
            'summary': {'stored_memories': len(memories), 'eligible_memories': sum(m['eligibility']['eligible'] for m in memories),
                        'eligible_notebook_entries': sum(n['eligibility']['eligible'] for n in notebook),
                        'eligible_reflection_entries': sum(n['eligibility']['eligible'] for n in reflected_items),
                        'history_candidates': sum(len(c['history_candidates']) for c in conversations)},
            'limitations': LIMITATIONS, 'reason_descriptions': REASONS,
            'memories': memories, 'notebook': notebook, 'reflection_notebook': reflected_items,
            'conversations': conversations, 'legacy_context': legacy}


def render_markdown(report):
    """Render exactly the same report fields as readable, safely quoted text."""
    def quoted(value):
        # Escaped Markdown keeps saved HTML/links inert when a reader renders the export.
        text = str(value).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        for char in ('\\', '`', '*', '_', '[', ']', '#', '|'):
            text = text.replace(char, '\\' + char)
        return '\n'.join('> ' + line for line in text.split('\n'))

    def item(entry):
        eligible = entry['eligibility']
        lines.extend(['', '### Saved item', '', quoted(entry['id']), '',
                      f"Eligible: {'yes' if eligible['eligible'] else 'no'}. Origin: {entry['origin']}. Retrieval: {entry['retrieval']}.",
                      '', quoted(entry['content'])])
        if entry.get('priority'):
            lines.extend(['', 'Retrieval priority (independent of confidence):', '', quoted(json.dumps(entry['priority'], sort_keys=True))])
        for code in eligible['reason_codes']:
            lines.extend(['', f"Reason: {code} — {report['reason_descriptions'][code]}"])
        for source in entry.get('sources', []):
            lines.extend(['', 'Evidence metadata:', quoted(json.dumps({k: v for k, v in source.items() if k != 'content'}, ensure_ascii=False, sort_keys=True))])
            lines.extend(['', quoted(f"Source: conversation {source.get('session_id', '?')}, message {source.get('message_id', '?')}, "
                          f"{source.get('role', 'unknown role')}, {source['status']}. ")])
            if 'content' in source:
                lines.extend(['', quoted(source['content']), '', f"Recorded SHA-256: {source['recorded_sha256']}",
                              f"Current message SHA-256: {source['sha256']}"])
        if 'provenance' in entry:
            lines.extend(['', 'Provenance:', '', quoted(json.dumps(entry['provenance'], ensure_ascii=False, sort_keys=True))])
        for key in ('type', 'confidence', 'created_at', 'updated_at', 'section', 'uncertainty', 'user_edited'):
            if key in entry:
                lines.extend(['', f'{key}:', quoted(entry[key])])

    lines = ['# What is known about me', '', quoted(report['scope']['name']), '',
             quoted(f"Scope: {report['scope']['id']}"), f"Generated: {report['generated_at']}",
             f"Schema: {report['schema_version']}", '', 'Recall:', quoted(json.dumps(report['recall'], sort_keys=True)),
             '', 'Counts:', quoted(json.dumps(report['summary'], sort_keys=True)), '', '## Reading this report']
    for limitation in report['limitations']:
        lines.extend(['', limitation])
    for heading, key in (('Saved memories', 'memories'), ('Current notebook — derived aggregate', 'notebook'),
                         ('Reflection notebook — source-tracked derived entries', 'reflection_notebook'),
                         ('Default companion legacy context', 'legacy_context')):
        lines.extend(['', '## ' + heading])
        if not report[key]:
            lines.extend(['', 'None.'])
        for entry in report[key]:
            item(entry)
    lines.extend(['', '## Conversation context candidates'])
    for conversation in report['conversations']:
        lines.extend(['', f"### Conversation {conversation['session_id']}", '',
                      quoted(json.dumps({k: v for k, v in conversation.items() if k != 'history_candidates'}, ensure_ascii=False, sort_keys=True))])
        for message in conversation['history_candidates']:
            lines.extend(['', quoted(f"Message {message['message_id']} ({message['role']}), SHA-256 {message['sha256']}:"), '', quoted(message['content'])])
    if not report['conversations']:
        lines.extend(['', 'None.'])
    return '\n'.join(lines) + '\n'
