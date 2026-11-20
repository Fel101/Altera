"""Companion memory provenance, correction and explicit recall boundaries.

Conversation text remains visible after forgetting a memory. A recall boundary
keeps earlier conversation text and derived notes out of future model prompts.
Remaining memories are retained; correcting one is an explicit user assertion.
"""
from contextlib import contextmanager
from datetime import datetime, UTC
import hashlib
import json
import re


import logging

LOGGER = logging.getLogger(__name__)
REBUILT_NOTICE = ('Automatic memories were rebuilt from the current eligible chat. The chat model read it when it was '
                  'available; otherwise the phrase matcher stood in and those memories are marked as such. Added and '
                  'corrected memories remain, changed sources need review, and the notes will be written afresh.')
REBUILD_KEY = 'memory_rebuild:'      # + owner: {state: running|done|changed|error, removed, added, process}


class KnowledgeError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def references(metadata):
    try:
        value = json.loads((metadata or {}).get('source_refs') or '[]')
        return value if isinstance(value, list) and len(value) <= 10000 else [{'invalid': True}]
    except (TypeError, ValueError):
        return [{'invalid': True}]


def reference(message):
    return {'message_id': message.id, 'session_id': message.session_id,
            'sha256': hashlib.sha256(message.content.encode('utf-8')).hexdigest()}


def attach_sources(store, memory_id, refs):
    old = store.collection.get(ids=[memory_id])
    if not old.get('ids'):
        return
    metadata = old['metadatas'][0] or {}
    if metadata.get('user_context') == 'manual_corrected':
        return
    combined = {json.dumps(r, sort_keys=True): r for r in references(metadata) + refs}
    store.collection.update(ids=[memory_id], metadatas=[{**metadata, 'source_refs': json.dumps(list(combined.values()))}])


def source_status(memory, store):
    owner = (memory.get('metadata') or {}).get('avatar_id') or ''
    result = []
    for ref in references(memory.get('metadata')):
        if (not isinstance(ref, dict) or type(ref.get('message_id')) is not int or not 0 < ref['message_id'] < 2 ** 63
                or type(ref.get('session_id')) is not int or not 0 < ref['session_id'] < 2 ** 63
                or not isinstance(ref.get('sha256'), str) or not re.fullmatch(r'[0-9a-f]{64}', ref['sha256'])):
            result.append({'status': 'unavailable'})
            continue
        message = store.get_message(ref.get('message_id'))
        session = store.get_session(message.session_id) if message else None
        # Message IDs and content hashes survive a split/merge. The recorded
        # session is the original location; current ownership must still match.
        valid_scope = session and (session.avatar_id or '') == owner
        item = {'message_id': ref.get('message_id'), 'session_id': ref.get('session_id'), 'status': 'unavailable'}
        if valid_scope:
            item.update(content=message.content, role=message.role, session_id=message.session_id,
                        status='current' if reference(message)['sha256'] == ref.get('sha256') else 'changed')
        result.append(item)
    return result


def can_recall(memory, store):
    sources = source_status(memory, store)
    if not sources:
        return (memory.get('metadata') or {}).get('user_context') in {'manual', 'manual_corrected'}
    return all(s['status'] == 'current' for s in sources)


def memory_review_hash(memory):
    """Bind a review to the saved fact and all of its metadata."""
    value = {'id': memory['id'], 'content': memory['content'], 'metadata': memory.get('metadata') or {}}
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()




def floor(store, owner):
    return int((store.get_key('knowledge_recall:' + (owner or '@default')) or {}).get('through') or 0)


def sessions(store, owner):
    conn = store._connect()
    try:
        return [r[0] for r in conn.execute("SELECT id FROM sessions WHERE COALESCE(avatar_id,'')=?", (owner or '',))]
    finally:
        conn.close()


def latest_message_id(store, owner):
    conn = store._connect()
    try:
        return conn.execute("SELECT max(m.id) FROM messages m JOIN sessions s ON s.id=m.session_id WHERE COALESCE(s.avatar_id,'')=?", (owner or '',)).fetchone()[0] or 0
    finally:
        conn.close()


def dirty(store, owner):
    return bool((store.get_key('knowledge_dirty_owner:' + (owner or '@default')) or {}).get('pending')) or any(
        (store.get_key(f'knowledge_dirty:{sid}') or {}).get('pending') for sid in sessions(store, owner))


def prompt_messages(store, session_id, messages, *, allow_derived=True, snapshot=False):
    session = store.get_session(session_id)
    through = floor(store, session.avatar_id if session else '')
    from core.presence import filter_messages
    return filter_messages(store, session_id, [m for m in messages if m.id > through],
                           exclude_derived=not allow_derived, snapshot=snapshot)


class KnowledgeService:
    def __init__(self, store, manager, notes_service=None, writer=None):
        self.ds, self.manager, self.notes, self.writer = store, manager, notes_service, writer

    @contextmanager
    def changing(self, owner, additional_owner=None):
        from core.turns import get_turn_service
        from core.state_gate import STATE, request_lease
        with STATE.lock:
            if STATE.draining or STATE.maintenance or STATE.fault or STATE.owned_jobs:
                raise KnowledgeError('Altera is updating saved data. Try again shortly.', 409)
        turns = get_turn_service(self.ds)
        owners = {owner} | ({additional_owner} if additional_owner is not None else set())
        # Submission and memory mutations serialize. Legacy HTTP chats also hold request leases.
        with turns.lock:
            own_lease = request_lease()
            with STATE.lock:
                if STATE.draining or STATE.maintenance or STATE.fault:
                    raise KnowledgeError('Altera is updating saved data. Try again shortly.', 409)
                if any(label in {'request:core_api.chat', 'request:core_api.chat_stream', 'altera-reply'} for label in STATE.active.values()):
                    raise KnowledgeError('Wait for the active reply to finish before changing memory.', 409)
                if STATE.owned_jobs or set(STATE.active) - ({own_lease.key} if own_lease is not None else set()):
                    raise KnowledgeError('Altera is finishing another request or background task. Try changing memory again shortly.', 409)
                STATE.maintenance = True  # Reserve before checking: a new legacy HTTP chat cannot race this mutation.
            try:
                conn = self.ds._connect()
                try:
                    if any(conn.execute("SELECT 1 FROM chat_turns t JOIN sessions s ON s.id=t.session_id WHERE COALESCE(s.avatar_id,'')=? AND t.status IN ('sending','streaming')", (item,)).fetchone() for item in owners):
                        raise KnowledgeError('Wait for the active reply to finish before changing memory.', 409)
                finally:
                    conn.close()
                if self.notes and any(owners):
                    with self.notes._lock:
                        if self.notes._busy.intersection(owners):
                            raise KnowledgeError('Wait for this companion’s notes update to finish.', 409)
                        yield
                else:
                    yield
            finally:
                with STATE.lock:
                    STATE.maintenance = False

    def _memories(self, owner):
        return [m for m in self.manager.store.get_all(avatar_id=owner)
                if ((m.get('metadata') or {}).get('avatar_id') or '') == owner]

    def get(self, owner, memory_id):
        found = next((m for m in self._memories(owner) if m['id'] == memory_id), None)
        if not found:
            raise KnowledgeError('Memory not found for this companion.', 404)
        return found

    def inventory(self, owner):
        from core.salience import describe
        memories = []
        for m in self._memories(owner):
            sources = source_status(m, self.ds)
            context = (m.get('metadata') or {}).get('user_context') or ''
            memories.append({**m, 'sources': sources, 'priority': describe(m.get('metadata')),
                             'content_sha256': hashlib.sha256(m['content'].encode('utf-8')).hexdigest(),
                             'review_sha256': memory_review_hash(m),
                             'needs_review': not can_recall(m, self.ds),
                             'source_kind': 'corrected by you' if context == 'manual_corrected' else
                             (('saved by you from a message' if context == 'manual' else 'extracted from a message') if sources
                              else ('added by you' if context == 'manual' else 'legacy — source unavailable'))})
        return {'memories': memories, 'owner': owner,
                'dataset_epoch': (self.ds.get_key('dataset_epoch') or self.ds.dataset_epoch())['value'],
                'review_through': latest_message_id(self.ds, owner),
                'repair_pending': dirty(self.ds, owner), 'recall_after': floor(self.ds, owner),
                'rebuild': self.rebuild_state(owner),
                'repair_available': any(m['needs_review'] and (m.get('metadata') or {}).get('user_context') == 'auto_extracted' for m in memories),
                'notes': self.notes.status(owner) if self.notes and owner else None}

    def prioritize(self, owner, memory_id, *, epoch, revision, content_sha256, changes):
        """Adjust retrieval priority without correcting a fact or clearing knowledge."""
        from core.salience import describe
        if not isinstance(changes, dict) or not changes or not set(changes) <= {'pinned', 'salience'}:
            raise KnowledgeError('Choose pinned and/or salience priority fields.')
        if 'pinned' in changes and type(changes['pinned']) is not bool:
            raise KnowledgeError('Pinned must be true or false.')
        if 'salience' in changes and (type(changes['salience']) is not int or not 1 <= changes['salience'] <= 5):
            raise KnowledgeError('Salience must be an integer from 1 through 5.')
        if not isinstance(epoch, str) or epoch != (self.ds.get_key('dataset_epoch') or self.ds.dataset_epoch()).get('value'):
            raise KnowledgeError('Altera’s data changed. Reload before changing priority.', 409)
        with self.changing(owner):
            if epoch != (self.ds.get_key('dataset_epoch') or {}).get('value'):
                raise KnowledgeError('Altera’s data changed. Reload before changing priority.', 409)
            item = self.get(owner, memory_id)
            old = item.get('metadata') or {}
            priority = describe(old)
            if (type(revision) is not int or revision != priority['revision'] or not isinstance(content_sha256, str) or
                    content_sha256 != hashlib.sha256(item['content'].encode('utf-8')).hexdigest()):
                raise KnowledgeError('This memory changed. Reload before changing priority.', 409)
            now = datetime.now(UTC).isoformat()
            updated = {**old, **changes, 'priority_revision': revision + 1, 'priority_updated_at': now,
                       'salience_anchor_at': now}
            self.manager.store.collection.update(ids=[memory_id], metadatas=[updated])
            return {'id': memory_id, 'priority': describe(updated), 'content_sha256': content_sha256}

    def _pending(self, owner, value):
        self.ds.set_key('knowledge_dirty_owner:' + (owner or '@default'), {'pending': value})
        for sid in sessions(self.ds, owner):
            state = self.ds.get_key(f'knowledge_dirty:{sid}') or {}
            self.ds.set_key(f'knowledge_dirty:{sid}', {**state, 'pending': value})

    def _clear_notes(self, owner):
        if self.notes and owner:
            try:
                self.notes.store.clear(owner)
            except OSError:
                raise KnowledgeError('Notebook cleanup could not be confirmed. Knowledge remains paused; review the notebook before retrying.', 409) from None
        elif not owner:
            # These legacy callbacks have no source IDs and may contain copies of edited/deleted facts.
            for key in ('jokes_and_references', 'humor_style', 'user_opinions', 'communication_style'):
                self.ds.set_key(key, {})
        conn = self.ds._connect()
        try:
            with conn:
                conn.execute("UPDATE reflection_notebook SET state='stale',text='',uncertainty='',sources='[]',snapshot='{}' WHERE avatar_id=?", (owner or '',))
                conn.execute('DELETE FROM reflection_reports WHERE avatar_id=?', (owner or '',))
        finally:
            conn.close()

    def _boundary(self, owner):
        through = latest_message_id(self.ds, owner)
        self.ds.set_key('knowledge_recall:' + (owner or '@default'), {'through': max(through, floor(self.ds, owner)),
                                                                'changed_at': datetime.now(UTC).isoformat()})
        from core.voice_notes import get_voice_note_service
        voice_notes = get_voice_note_service(self.ds)
        voice_notes.invalidate_owner(owner)
        conn = self.ds._connect()
        try:
            with conn:
                from core.expression import invalidate_owner as invalidate_expression
                from core.presence import invalidate_owner as invalidate_presence
                invalidate_expression(conn, owner)
                invalidate_presence(conn, owner, 'Recall boundary changed.')
                from core.episodes import invalidate_owner as invalidate_episodes
                from core.reflection import invalidate_owner as invalidate_reflection
                invalidate_episodes(conn, owner, 'Recall boundary changed.')
                invalidate_reflection(conn, owner, 'Recall boundary changed.')
        finally:
            conn.close()

    def remember(self, owner, content, memory_type='fact', confidence=1., source_id=None, session_id=None):
        refs = []
        if session_id is not None:
            if type(session_id) is not int:
                raise KnowledgeError('session_id must be an integer.')
            info = self.ds.get_session(session_id)
            if not info or (info.avatar_id or '') != owner:
                raise KnowledgeError('Conversation not found for this companion.', 404)
        if source_id is not None:
            if isinstance(source_id, bool) or not isinstance(source_id, int):
                raise KnowledgeError('source_message_id must be an integer.')
            message = self.ds.get_message(source_id)
            info = self.ds.get_session(message.session_id) if message else None
            if not info or (info.avatar_id or '') != owner or (session_id is not None and message.session_id != session_id):
                raise KnowledgeError('Source message not found for this companion.', 404)
            refs = [reference(message)]
            session_id = message.session_id
        with self.changing(owner):
            if refs:
                current = self.ds.get_message(source_id)
                if current is None or reference(current) != refs[0]:
                    raise KnowledgeError('The source changed. Reload and review it before remembering.', 409)
            return self.manager.store.add_memory(content=content, memory_type=memory_type, confidence=confidence,
                                                 session_id=session_id, avatar_id=owner, user_context='manual', source_refs=refs)



    def change(self, owner, memory_id, content=None, confidence=None, forget=False, new_owner=None):
        affected = {owner} | ({new_owner} if new_owner is not None else set())
        with self.changing(owner, new_owner):
            record = self.get(owner, memory_id)
            prior_pending = {item: dirty(self.ds, item) for item in affected}
            # Mark first: if any store/file operation fails, stale derived knowledge stays excluded.
            for item in affected:
                self._pending(item, True)
                self._boundary(item)
                self._clear_notes(item)
            if forget:
                self.manager.store.delete_memory(memory_id)
            else:
                metadata = {**(record.get('metadata') or {}), 'source_refs': '[]', 'user_context': 'manual_corrected',
                            'updated_at': datetime.now(UTC).isoformat(), 'avatar_id': new_owner if new_owner is not None else owner,
                            'confidence': confidence if confidence is not None else 1.}
                if new_owner is not None and new_owner != owner:
                    metadata['session_id'] = ''
                args = {'ids': [memory_id], 'metadatas': [metadata]}
                if content is not None:
                    args['documents'] = [content]
                self.manager.store.collection.update(**args)
            for item in affected:
                self._pending(item, prior_pending[item])
        return {'status': 'ok', 'recall_after': floor(self.ds, owner), 'notes_cleared': bool(owner),
                'repair_pending': any(prior_pending.values()),
                'notice': 'Earlier conversation text remains visible but is no longer sent to the model. Companion notes and their history were cleared; other memories remain.'}

    # ---- Rebuild in the background ------------------------------------------------------------------------------------
    def rebuild_state(self, owner):
        """The last Rebuild for this person, or None. A 'running' record left by a process that has since stopped is
        reported as stopped, so the person can start again."""
        from core.turns import PROCESS_ID
        state = self.ds.get_key(REBUILD_KEY + (owner or '@default'))
        if state and state.get('state') == 'running' and state.get('process') != PROCESS_ID:
            return {**state, 'state': 'error', 'error': 'Altera stopped before the rebuild finished. Rebuild again.'}
        return state

    def _revisions(self, owner):
        return {sid: int((self.ds.get_key(f'knowledge_dirty:{sid}') or {}).get('revision') or 0)
                for sid in sessions(self.ds, owner)}

    def _start_rebuild(self, owner, revisions, removed):
        writer = self.writer
        if writer.sync or getattr(writer.notes, 'sync', False):
            self._rebuild_job(owner, revisions, removed)
            return self.rebuild_state(owner)
        from core.state_gate import start_job
        try:
            start_job(self._rebuild_job, (owner, revisions, removed), name='altera-memory-rebuild')
        except Exception:
            LOGGER.exception('Could not start the memory rebuild')
            self._record_rebuild(owner, 'error', removed, error='The rebuild could not start. Try again.')
        return None

    def _rebuild_job(self, owner, revisions, removed):
        import time
        writer, reserved, result = self.writer, False, None
        try:
            for _ in range(120):          # a notes or memory pass that began just before Rebuild finishes first
                reserved = writer._reserve(owner)
                if reserved:
                    break
                time.sleep(0.5)
            if not reserved:
                result = {'ok': False, 'error': 'Another memory pass for this person did not finish. Rebuild again.'}
            else:
                result = writer.run(owner, force=True)
        except Exception:
            LOGGER.exception('Memory rebuild failed')
            result = {'ok': False, 'error': 'The rebuild stopped with an error. Rebuild again.'}
        finally:
            if reserved:
                writer._release(owner)
        self._finish_rebuild(owner, revisions, removed, result or {})

    def _finish_rebuild(self, owner, revisions, removed, result):
        """Resume derived knowledge, unless the chat was edited while the model was reading it: then what was written may
        come from text that has since changed, so it stays paused and the person is asked to rebuild again."""
        added = int(result.get('stored') or 0)
        if not result.get('ok'):
            self._record_rebuild(owner, 'error', removed, added, error=result.get('error') or 'The rebuild did not finish.')
            return
        conn = self.ds._connect()
        try:
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                states = {}
                for sid in revisions:
                    row = conn.execute('SELECT value FROM user_profile WHERE key=?', (f'knowledge_dirty:{sid}',)).fetchone()
                    states[sid] = json.loads(row[0]) if row else {}
                if any(int(states[sid].get('revision') or 0) != revision for sid, revision in revisions.items()):
                    changed = True
                else:
                    changed = False
                    for sid, state in states.items():
                        conn.execute('INSERT OR REPLACE INTO user_profile(key,value,updated_at) VALUES(?,?,CURRENT_TIMESTAMP)',
                                     (f'knowledge_dirty:{sid}', json.dumps({**state, 'pending': False})))
                    conn.execute('INSERT OR REPLACE INTO user_profile(key,value,updated_at) VALUES(?,?,CURRENT_TIMESTAMP)',
                                 ('knowledge_dirty_owner:' + (owner or '@default'), json.dumps({'pending': False})))
        finally:
            conn.close()
        if changed:
            self._record_rebuild(owner, 'changed', removed, added,
                                 error='The chat changed while the model was reading it. Rebuild again to use memories.')
        else:
            self._record_rebuild(owner, 'done', removed, added)

    def _record_rebuild(self, owner, state, removed, added=0, error=''):
        from core.turns import PROCESS_ID
        self.ds.set_key(REBUILD_KEY + (owner or '@default'), {'state': state, 'removed': removed, 'added': added,
                                                               'error': error, 'process': PROCESS_ID,
                                                               'at': datetime.now(UTC).isoformat()})

    def repair(self, owner):
        with self.changing(owner):
            running = self.rebuild_state(owner)
            if running and running.get('state') == 'running':
                raise KnowledgeError('Automatic memories are already being rebuilt. Wait for it to finish.', 409)
            self._pending(owner, True)
            self._clear_notes(owner)
            removed = 0
            for m in self._memories(owner):
                if (m.get('metadata') or {}).get('user_context') == 'auto_extracted':
                    refs = references(m.get('metadata'))
                    if refs and can_recall(m, self.ds) and all(r['message_id'] <= floor(self.ds, owner) for r in refs):
                        continue  # Keep still-valid memories retained across an earlier recall boundary.
                    self.manager.store.delete_memory(m['id'])
                    removed += 1
            added, revisions = 0, {}
            if self.writer is not None:
                # The model reads the chat in the background: it can take minutes over a long history, and holding the
                # maintenance gate that long would refuse every chat in the house. Derived knowledge stays paused (pending)
                # until it finishes; see _finish_rebuild.
                self.writer.mark_through(owner, floor(self.ds, owner))
                revisions = self._revisions(owner)
                self._record_rebuild(owner, 'running', removed)
            else:
                # A caller without a writer (tests and pages that only change memory) gets the phrase matcher.
                for sid in sessions(self.ds, owner):
                    for message in self.ds.get_messages_after(sid, floor(self.ds, owner)):
                        if message.role == 'user':
                            added += len(self.manager.add_conversation_memories(message.content, session_id=sid, avatar_id=owner,
                                                                               source_refs=[reference(message)]))
                self._pending(owner, False)
        if self.writer is not None:
            finished = self._start_rebuild(owner, revisions, removed)
            if finished is not None:          # run inline (tests)
                added = int(finished.get('added') or 0)
                return {'status': 'ok' if finished.get('state') == 'done' else 'error', 'removed': removed, 'added': added,
                        'notice': finished.get('error') or REBUILT_NOTICE}
            return {'status': 'started', 'removed': removed, 'added': 0, 'rebuilding': True,
                    'notice': 'Rebuilding automatic memories in the background: the chat model is reading the current eligible '
                              'chat. You can keep chatting; derived memories and notes stay paused until it finishes. Added and '
                              'corrected memories remain, and changed sources need review.'}
        return {'status': 'ok', 'removed': removed, 'added': added, 'notice': REBUILT_NOTICE}

    def clear(self, owner):
        with self.changing(owner):
            self._pending(owner, True)
            self._boundary(owner)
            self._clear_notes(owner)
            for memory in self._memories(owner):
                self.manager.store.delete_memory(memory['id'])
            self._pending(owner, False)
        return {'status': 'ok', 'recall_after': floor(self.ds, owner)}


def get_knowledge_service():
    from core.datastore import get_datastore
    from core.memory import get_memory_manager, get_memory_writer
    from core.notes import get_notes_service
    return KnowledgeService(get_datastore(), get_memory_manager(), get_notes_service(), writer=get_memory_writer())
