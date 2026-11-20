"""Memory edits must change what the model can recall, with companion isolation."""
import json
from types import SimpleNamespace

import pytest

from core import memory, notes
from core.knowledge import KnowledgeError, KnowledgeService, can_recall, dirty, floor, reference
from core.memory import MemoryDeduplicator, MemoryExtractor, MemoryManager, MemoryStore
from core.request_security import install_request_security
from core.turns import get_turn_service
from test_blueprints import create_app
from test_companion_api import active_avatar, engine, tmp_store, world  # noqa: F401
from test_companion_images import make_forge
from test_companion_notes import FakeCollection, answer


class Collection(FakeCollection):
    def get(self, where=None, ids=None, **kwargs):
        ids = [i for i in (ids if ids is not None else self._match(where)) if i in self.rows]
        return {'ids': ids, 'documents': [self.rows[i][0] for i in ids], 'metadatas': [self.rows[i][1] for i in ids]}

    def update(self, ids, metadatas=None, documents=None):
        for n, item in enumerate(ids):
            content, metadata = self.rows[item]
            self.rows[item] = (documents[n] if documents else content, {**metadata, **(metadatas[n] if metadatas else {})})

    def query(self, query_texts, n_results, where=None):
        query = set(query_texts[0].lower().split())
        def distance(item):
            words = set(self.rows[item][0].lower().split())
            return 2 * (1 - len(words & query) / max(1, len(words | query)))
        ids = sorted(self._match(where), key=distance)[:n_results]
        return {'ids': [ids], 'documents': [[self.rows[i][0] for i in ids]],
                'metadatas': [[self.rows[i][1] for i in ids]], 'distances': [[distance(i) for i in ids]]}


@pytest.fixture
def knowledge_world(world, engine, monkeypatch):
    a = active_avatar(world)['id']
    make_forge(world.forge, npc_id='ana-test-123456', first='Ana')
    b = world.avatars.import_from_forge('ana-test-123456')['id']
    a_sid, b_sid = (world.store.ensure_avatar_session(owner, owner) for owner in (a, b))
    shared_sid = world.store.create_session('Default')
    manager = MemoryManager.__new__(MemoryManager)
    manager.store = MemoryStore.__new__(MemoryStore)
    manager.store._collection, manager.store._client = Collection(), None
    manager.extractor, manager.deduplicator = MemoryExtractor(), MemoryDeduplicator()
    monkeypatch.setattr(memory, 'memory_manager', manager)
    calls = []
    def llm(messages):
        calls.append(messages)
        return answer('- New conversation only', '- A fresh start')
    notebook = notes.NotesService(notes.NotesStore(world.avatars), lambda: world.store, llm=llm, sync=True)
    notes.set_notes_service(notebook)
    def no_memory_model(messages):
        raise RuntimeError('no chat model in this test')
    writer = memory.MemoryWriter(memory.get_memory_manager, lambda: world.store, config_getter=dict, llm=no_memory_model,
                                 notes=notebook, sync=True)
    memory.set_memory_writer(writer)
    api = create_app('knowledge', 'core_api', 'pages', 'sessions')
    install_request_security(api)
    return SimpleNamespace(ds=world.store, manager=manager, notes=notebook, calls=calls, a=a, b=b,
                           a_sid=a_sid, b_sid=b_sid, shared_sid=shared_sid, writer=writer,
                           service=KnowledgeService(world.store, manager, notebook, writer=writer),
                           client=api.test_client(), engine=engine)


def automatic(w, text='I prefer tea without sugar.', owner=None, sid=None):
    owner, sid = owner or w.a, sid or w.a_sid
    mid = w.ds.save_message(sid, 'user', text)
    ids = w.manager.add_conversation_memories(text, session_id=sid, avatar_id=owner, source_refs=[reference(w.ds.get_message(mid))])
    return mid, ids


def test_remember_records_current_source_and_rejects_cross_companion_source(knowledge_world):
    w = knowledge_world
    mid = w.ds.save_message(w.a_sid, 'user', 'Please remember my preferred tea.')
    response = w.client.post(f'/api/knowledge/scopes/{w.a}/memories', json={
        'content': 'Prefers tea', 'source_message_id': mid, 'session_id': w.a_sid})
    assert response.status_code == 201
    item = w.service.inventory(w.a)['memories'][0]
    assert item['sources'][0]['content'] == 'Please remember my preferred tea.'
    assert item['sources'][0]['status'] == 'current'
    assert w.client.post(f'/api/knowledge/scopes/{w.b}/memories', json={'content': 'Wrong owner', 'source_message_id': mid}).status_code == 404
    assert not w.service.inventory(w.b)['memories']


def test_default_companion_cannot_retrieve_other_companion_memories(knowledge_world):
    w = knowledge_world
    w.service.remember(w.a, 'Mara-only tea fact')
    default = w.service.remember('', 'Default-only tea fact')
    assert [m['id'] for m in w.manager.get_relevant_memories('tea', avatar_id='')] == [default]
    assert [m['id'] for m in w.service.inventory('')['memories']] == [default]


def test_a_batch_links_the_memory_to_every_message_it_came_from(knowledge_world):
    w = knowledge_world
    text = 'I prefer tea without sugar.'
    ids = [w.ds.save_message(w.a_sid, 'user', text) for _ in range(2)]
    w.writer.run(w.a)
    inventory = w.service.inventory(w.a)['memories']
    assert len(inventory) == 1
    assert [s['message_id'] for s in inventory[0]['sources']] == ids
    assert inventory[0]['metadata']['extracted_by'] == 'phrases'


def test_each_pass_reads_only_messages_after_the_last_read_point(knowledge_world):
    w = knowledge_world
    text = 'I prefer tea without sugar.'
    first = w.ds.save_message(w.a_sid, 'user', text)
    w.writer.run(w.a)
    item = w.service.inventory(w.a)['memories'][0]
    assert [source['message_id'] for source in item['sources']] == [first]
    second = w.ds.save_message(w.a_sid, 'user', text)
    w.writer.run(w.a)
    item = w.service.inventory(w.a)['memories'][0]
    assert [source['message_id'] for source in item['sources']] == [first, second]


def test_a_reply_asks_for_a_memory_pass_instead_of_extracting_one_message(knowledge_world):
    w = knowledge_world
    scheduled = []
    w.writer.schedule = lambda owner: scheduled.append(owner) or None
    w.engine.send_message('I prefer tea without sugar.', w.a_sid)
    assert scheduled == [w.a]
    assert w.service.inventory(w.a)['memories'] == []          # nothing is written on the reply's own path
    w.writer.run(w.a)
    saved = [m.id for m in w.ds.get_messages(w.a_sid) if m.role == 'user']
    item = w.service.inventory(w.a)['memories'][0]
    assert [source['message_id'] for source in item['sources']] == saved


def test_correct_clears_note_copies_and_old_model_history_but_preserves_other_companion(knowledge_world):
    w = knowledge_world
    mid, ids = automatic(w)
    w.ds.save_message(w.a_sid, 'assistant', 'Yes, you prefer tea without sugar.')
    other = w.service.remember(w.b, 'Ana knows about coffee')
    w.notes.store.write(w.a, about_you='- Prefers tea', our_story='- They discussed tea')
    w.notes.store.write(w.a, about_you='- Prefers tea without sugar')  # archived copy too
    w.notes.store.write(w.b, about_you='- Coffee', our_story='- Ana’s story')
    old_b = w.notes.store.read(w.b)
    result = w.client.put(f'/api/knowledge/scopes/{w.a}/memories/{ids[0]}', json={'content': 'I prefer coffee without sugar.'})
    assert result.status_code == 200
    assert not w.notes.store.dir(w.a).exists()
    assert w.notes.store.read(w.b) == old_b
    assert w.service.get(w.b, other)['content'] == 'Ana knows about coffee'
    assert w.ds.get_message(mid).content == 'I prefer tea without sugar.'  # visible history is retained
    current = w.ds.save_message(w.a_sid, 'user', 'What do you remember?')
    context = w.engine.build_context(w.a_sid)
    history = w.engine._history_for(w.a_sid, context)
    assert [m['content'] for m in history] == ['What do you remember?']
    assert 'I prefer coffee without sugar.' in context.system_prompt and 'I prefer tea without sugar.' not in context.system_prompt
    corrected = w.service.inventory(w.a)['memories'][0]
    assert corrected['source_kind'] == 'corrected by you' and corrected['sources'] == []
    assert corrected['metadata']['confidence'] == 1.
    assert result.json['recall_after'] < current


def test_forget_cannot_be_relearned_by_notes_or_repair_from_old_transcript(knowledge_world):
    w = knowledge_world
    _, ids = automatic(w)
    w.ds.save_message(w.a_sid, 'assistant', 'I will remember that you prefer tea.')
    result = w.service.change(w.a, ids[0], forget=True)
    assert result['status'] == 'ok'
    w.ds.save_message(w.a_sid, 'user', 'A new topic: the weather.')
    assert w.service.repair(w.a)['added'] == 0
    assert w.notes.run_now(w.a, w.a_sid)['ok']
    assert w.calls and 'prefer tea' not in json.dumps(w.calls).lower()
    assert not w.service.inventory(w.a)['memories']
    assert len(w.ds.get_messages(w.a_sid)) == 3


def test_model_written_memories_keep_sources_and_can_be_corrected_or_forgotten(knowledge_world):
    w = knowledge_world
    mid = w.ds.save_message(w.a_sid, 'user', "I'm a bit scared of the sea since the storm last night.")
    w.writer._llm = lambda messages: json.dumps([
        {'content': 'Is afraid of the sea since a storm on 2026-11-16', 'type': 'event', 'confidence': 0.9, 'from': [mid]}])
    assert w.writer.run(w.a)['stored'] == 1
    item = w.service.inventory(w.a)['memories'][0]
    assert item['metadata']['extracted_by'] == 'model' and item['metadata']['type'] == 'event'
    assert [source['message_id'] for source in item['sources']] == [mid]
    assert item['sources'][0]['status'] == 'current'

    # Sources: editing the message marks the memory for review.
    w.ds.change_message(mid, w.a_sid, content='I am fine now.')
    item = w.service.inventory(w.a)['memories'][0]
    assert item['needs_review'] and item['sources'][0]['status'] == 'changed'

    # Correct replaces the fact; Forget removes it.
    w.service.change(w.a, item['id'], content='Is not afraid of the sea')
    item = w.service.inventory(w.a)['memories'][0]
    assert item['content'] == 'Is not afraid of the sea' and item['source_kind'] == 'corrected by you'
    assert w.service.change(w.a, item['id'], forget=True)['status'] == 'ok'
    assert w.service.inventory(w.a)['memories'] == []


def test_message_edit_repair_replaces_auto_facts_and_marks_manual_source_for_review(knowledge_world):
    w = knowledge_world
    mid, ids = automatic(w)
    manual = w.service.remember(w.a, 'A manually selected tea fact', source_id=mid)
    w.notes.store.write(w.a, about_you='- Tea')
    w.ds.change_message(mid, w.a_sid, content='I prefer coffee without sugar.')
    assert dirty(w.ds, w.a) and not w.manager.get_relevant_memories('tea', avatar_id=w.a)
    repaired = w.service.repair(w.a)
    assert repaired['removed'] == 1 and not dirty(w.ds, w.a)
    inventory = w.service.inventory(w.a)['memories']
    assert any('coffee' in m['content'] and m['sources'][0]['status'] == 'current' for m in inventory)
    selected = next(m for m in inventory if m['id'] == manual)
    assert selected['needs_review'] and selected['sources'][0]['status'] == 'changed'
    assert not can_recall(selected, w.ds)
    assert all(m['id'] != manual for m in w.manager.get_relevant_memories('tea', avatar_id=w.a))
    assert not w.notes.store.dir(w.a).exists()


def test_delete_source_is_unavailable_and_cannot_be_used_as_memory(knowledge_world):
    w = knowledge_world
    mid, _ = automatic(w)
    w.ds.change_message(mid, w.a_sid, delete=True)
    item = w.service.inventory(w.a)['memories'][0]
    assert item['needs_review'] and item['sources'][0]['status'] == 'unavailable'
    w.service.repair(w.a)
    assert not w.service.inventory(w.a)['memories']


def test_active_reply_and_notes_writer_block_corrections(knowledge_world):
    w = knowledge_world
    item = w.service.remember(w.a, 'A fact')
    turns = get_turn_service(w.ds)
    with turns.connection() as conn:
        conn.execute("INSERT INTO chat_turns(request_id,session_id,fingerprint,process_id,status) VALUES('active-knowledge-test',?,'f','test','streaming')", (w.a_sid,))
    with pytest.raises(KnowledgeError, match='active reply'):
        w.service.change(w.a, item, forget=True)
    with turns.connection() as conn:
        conn.execute("UPDATE chat_turns SET status='saved'")
    w.notes._busy.add(w.a)
    try:
        with pytest.raises(KnowledgeError, match='notes update'):
            w.service.change(w.a, item, forget=True)
    finally:
        w.notes._busy.clear()
    assert w.service.get(w.a, item)['content'] == 'A fact'


def test_note_clear_failure_does_not_report_success_or_allow_stale_recall(knowledge_world, monkeypatch):
    w = knowledge_world
    item = w.service.remember(w.a, 'Private tea preference')
    def fail(owner):
        raise OSError('cannot clear notes')
    monkeypatch.setattr(w.notes.store, 'clear', fail)
    with pytest.raises(KnowledgeError, match='Notebook cleanup could not be confirmed') as error:
        w.service.change(w.a, item, forget=True)
    assert 'cannot clear notes' not in str(error.value)
    assert dirty(w.ds, w.a)
    assert not w.manager.get_relevant_memories('tea', avatar_id=w.a)


def test_scoped_forget_confirmation_wrong_owner_and_origin_guard(knowledge_world):
    w = knowledge_world
    item = w.service.remember(w.a, 'One companion only')
    url = f'/api/knowledge/scopes/{w.a}/memories/{item}'
    assert w.client.delete(url, json={}).status_code == 400
    assert w.client.delete(url.replace(w.a, w.b), json={'confirm': 'forget'}).status_code == 404
    assert w.client.delete(url, json={'confirm': 'forget'}, headers={'Origin': 'https://other.example'}).status_code == 403
    assert w.client.delete(url, json={'confirm': 'forget'}).status_code == 200


def test_consolidation_keeps_source_lineage_and_refuses_cross_owner_groups(knowledge_world):
    w = knowledge_world
    first = w.ds.save_message(w.a_sid, 'user', 'I prefer tea without sugar.')
    second = w.ds.save_message(w.a_sid, 'user', 'I prefer strong tea without sugar.')
    for mid in (first, second):
        w.manager.store.add_memory('Likes tea', avatar_id=w.a, user_context='auto_extracted', source_refs=[reference(w.ds.get_message(mid))])
    groups = w.manager.deduplicator.find_duplicates(w.manager.store.get_all())
    assert w.manager.deduplicator.consolidate_duplicates(groups)
    item = w.service.inventory(w.a)['memories'][0]
    assert {s['message_id'] for s in item['sources']} == {first, second}
    other = w.service.remember(w.b, 'Likes tea')
    assert w.manager.deduplicator.consolidate_duplicates([{'memories': [item, w.service.get(w.b, other)], 'type': 'fact'}]) == []


def test_split_preserves_owner_source_ids_and_retires_replay(knowledge_world):
    w = knowledge_world
    start = w.ds.save_message(w.a_sid, 'user', 'Start')
    source, ids = automatic(w)
    original = w.ds.get_message(source)
    turns = get_turn_service(w.ds)
    with turns.connection() as conn:
        conn.execute("INSERT INTO chat_turns(request_id,session_id,fingerprint,process_id,status,user_message_id) VALUES('split-source-request',?,'f','test','saved',?)", (w.a_sid, source))
    response = w.client.post(f'/api/sessions/{w.a_sid}/split', json={'split_message_id': start})
    assert response.status_code == 200
    sid = response.json['new_session_id']
    assert w.ds.get_session(sid).avatar_id == w.a
    moved = w.ds.get_message(source)
    assert moved.session_id == sid and moved.content == original.content and moved.timestamp == original.timestamp
    assert w.service.inventory(w.a)['memories'][0]['sources'][0]['session_id'] == sid
    assert turns.get('split-source-request') is None and dirty(w.ds, w.a)
    with turns.connection() as conn:
        assert conn.execute("SELECT 1 FROM chat_request_tombstones WHERE request_id='split-source-request'").fetchone()


def test_cross_companion_merge_is_rejected_and_same_owner_sources_follow_move(knowledge_world):
    w = knowledge_world
    source, ids = automatic(w)
    turns = get_turn_service(w.ds)
    with turns.connection() as conn:
        conn.execute("INSERT INTO chat_turns(request_id,session_id,fingerprint,process_id,status,user_message_id) VALUES('merge-source-request',?,'f','test','saved',?)", (w.a_sid, source))
    assert w.client.post(f'/api/sessions/{w.b_sid}/merge', json={'target_session_id': w.a_sid}).status_code == 409
    target = w.ds.create_session('Another Mara conversation')
    w.ds.set_session_avatar(target, w.a)
    response = w.client.post(f'/api/sessions/{target}/merge', json={'target_session_id': w.a_sid})
    assert response.status_code == 200 and w.ds.get_message(source).session_id == target
    assert turns.get('merge-source-request') is None
    item = w.service.inventory(w.a)['memories'][0]
    assert item['sources'][0]['status'] == 'current' and item['sources'][0]['session_id'] == target


def test_repair_keeps_other_valid_memories_retained_before_recall_boundary(knowledge_world):
    w = knowledge_world
    _, ids = automatic(w)
    source = w.ds.save_message(w.a_sid, 'user', 'I enjoy hiking in the mountains.')
    keep = w.manager.store.add_memory('Enjoys hiking', avatar_id=w.a, user_context='auto_extracted', source_refs=[reference(w.ds.get_message(source))])
    w.service.change(w.a, ids[0], forget=True)
    w.service.repair(w.a)
    assert [m['id'] for m in w.service.inventory(w.a)['memories']] == [keep]


def test_memory_mutation_reserves_gate_against_new_legacy_chats(knowledge_world):
    from core.state_gate import STATE, StateBusy
    w = knowledge_world
    with w.service.changing(w.a):
        with pytest.raises(StateBusy):
            STATE.enter('request:core_api.chat')
    lease = STATE.enter('request:core_api.chat')
    try:
        with pytest.raises(KnowledgeError, match='active reply'):
            w.service.clear(w.a)
    finally:
        lease.release()


def test_deleting_last_conversation_keeps_derived_knowledge_paused(knowledge_world):
    w = knowledge_world
    automatic(w)
    w.notes.store.write(w.a, about_you='- Prefers tea')
    w.ds.delete_session(w.a_sid)
    assert dirty(w.ds, w.a) and not w.manager.get_relevant_memories('tea', avatar_id=w.a)
    new_sid = w.ds.ensure_avatar_session(w.a, 'A fresh chat')
    assert w.engine.build_context(new_sid).notes == {}
    w.service.repair(w.a)
    assert not w.service.inventory(w.a)['memories'] and not dirty(w.ds, w.a)


def test_companion_named_shared_does_not_collide_with_default_scope(knowledge_world, world):
    w = knowledge_world
    make_forge(world.forge, npc_id='shared', first='Shared')
    world.avatars.import_from_forge('shared')
    w.ds.ensure_avatar_session('shared', 'Shared')
    source = w.ds.save_message(w.shared_sid, 'user', 'A default-only fact')
    default = w.service.remember('', 'Default-only fact')
    separate = w.service.remember('shared', 'A separate companion’s fact')
    w.service.change('', default, forget=True)
    assert floor(w.ds, '') == source and floor(w.ds, 'shared') == 0
    assert w.client.get('/api/knowledge/scopes/_default/memories').json['memories'] == []
    assert w.client.get('/api/knowledge/scopes/shared/memories').json['memories'][0]['id'] == separate


def test_notes_job_cannot_publish_text_if_its_source_changed_during_generation(knowledge_world):
    w = knowledge_world
    mid, _ = automatic(w)
    def changed(messages):
        w.ds.change_message(mid, w.a_sid, content='A corrected source message')
        return answer('- Obsolete tea preference', '- Old story')
    w.notes._llm = changed
    assert not w.notes.run_now(w.a, w.a_sid)['ok']
    assert dirty(w.ds, w.a) and not any(w.notes.store.read(w.a).values())


def test_rebuild_does_not_merge_a_current_fact_into_a_stale_manual_copy(knowledge_world):
    w = knowledge_world
    mid, _ = automatic(w)
    manual = w.service.remember(w.a, 'I prefer tea without sugar', source_id=mid)
    w.ds.change_message(mid, w.a_sid, content='I prefer tea  without sugar.')
    w.service.repair(w.a)
    items = w.service.inventory(w.a)['memories']
    assert next(m for m in items if m['id'] == manual)['needs_review']
    automatic_memories = [m for m in items if m['metadata']['user_context'] == 'auto_extracted']
    assert len(automatic_memories) == 1 and not automatic_memories[0]['needs_review']
    assert not w.manager.deduplicator.find_duplicates(items)
    assert not w.manager.deduplicator.consolidate_duplicates([{'memories': items, 'type': 'fact'}])
    assert len(w.service.inventory(w.a)['memories']) == 2


def test_explicit_legacy_reassignment_clears_foreign_source_metadata(knowledge_world):
    w = knowledge_world
    _, ids = automatic(w)
    response = w.client.put('/api/memory/' + ids[0], json={'avatar_id': w.b, 'content': 'A fact explicitly moved to Ana'})
    assert response.status_code == 200
    assert not w.service.inventory(w.a)['memories']
    item = w.service.inventory(w.b)['memories'][0]
    assert item['metadata']['avatar_id'] == w.b and item['metadata']['session_id'] == '' and item['sources'] == []


def test_untracked_legacy_memories_stay_visible_for_review_but_cannot_reintroduce_old_facts(knowledge_world):
    w = knowledge_world
    legacy = w.manager.store.add_memory('An untracked older fact', avatar_id=w.a)
    w.manager.store.add_memory('An older automatic fact', avatar_id=w.a, user_context='auto_extracted')
    inventory = w.service.inventory(w.a)
    assert inventory['repair_available'] and all(m['needs_review'] for m in inventory['memories'])
    assert not w.manager.get_relevant_memories('fact', avatar_id=w.a)
    w.service.repair(w.a)
    assert [m['id'] for m in w.service.inventory(w.a)['memories']] == [legacy]
    assert not w.manager.get_relevant_memories('fact', avatar_id=w.a)
    w.service.change(w.a, legacy, content='A fact explicitly confirmed by the user')
    assert w.manager.get_relevant_memories('fact', avatar_id=w.a)[0]['id'] == legacy


@pytest.mark.parametrize('source_kind,legacy', [('user-edited', False), (None, True)])
def test_notebook_provenance_does_not_claim_manual_or_legacy_text_came_only_from_chat(knowledge_world, source_kind, legacy):
    w = knowledge_world
    automatic(w)
    w.notes.store.write(w.a, about_you='- An earlier note')
    if source_kind:
        w.notes.store.update_state(w.a, source_kind=source_kind)
    assert w.notes.run_now(w.a, w.a_sid)['ok']
    source = w.notes.status(w.a)['sources']
    assert source['includes_user_edits'] == bool(source_kind)
    assert source['includes_legacy_notes'] == legacy


@pytest.mark.parametrize('value', ['not-json', '{}', '[{"message_id": {}}]',
                                  json.dumps([{'message_id': 2 ** 100, 'session_id': 1, 'sha256': '0' * 64}])])
def test_malformed_provenance_is_excluded_instead_of_trusted(knowledge_world, value):
    w = knowledge_world
    item = w.service.remember(w.a, 'Bad source')
    w.manager.store.collection.update(ids=[item], metadatas=[{'source_refs': value}])
    assert w.service.inventory(w.a)['memories'][0]['needs_review']
    assert not w.manager.get_relevant_memories('source', avatar_id=w.a)


def _captured_rebuild(w, monkeypatch):
    """Run Rebuild the way the app does (in the background), but hold the job so the test decides when it runs."""
    import core.state_gate as state_gate
    jobs = []
    monkeypatch.setattr(w.writer, 'sync', False)
    monkeypatch.setattr(w.notes, 'sync', False)
    monkeypatch.setattr(state_gate, 'start_job', lambda target, args=(), name='': jobs.append((target, args)))
    return jobs


def test_rebuild_runs_in_the_background_without_holding_the_whole_app(knowledge_world, monkeypatch):
    from core.state_gate import STATE
    w = knowledge_world
    mid, _ = automatic(w)
    w.ds.change_message(mid, w.a_sid, content='I prefer coffee without sugar.')
    jobs = _captured_rebuild(w, monkeypatch)
    started = w.service.repair(w.a)
    assert started['status'] == 'started' and started['rebuilding'] is True
    assert STATE.maintenance is False                      # chats are not refused while the model reads
    assert dirty(w.ds, w.a)                                # derived knowledge stays paused until it finishes
    assert w.service.inventory(w.a)['rebuild']['state'] == 'running'
    with pytest.raises(KnowledgeError):
        w.service.repair(w.a)                              # one rebuild at a time
    target, args = jobs.pop()
    target(*args)
    assert not dirty(w.ds, w.a)
    assert w.service.inventory(w.a)['rebuild']['state'] == 'done'
    assert any('coffee' in m['content'] for m in w.service.inventory(w.a)['memories'])


def test_a_chat_edited_during_rebuild_stays_paused(knowledge_world, monkeypatch):
    w = knowledge_world
    mid, _ = automatic(w)
    w.ds.change_message(mid, w.a_sid, content='I prefer coffee without sugar.')
    jobs = _captured_rebuild(w, monkeypatch)
    w.service.repair(w.a)
    w.ds.change_message(mid, w.a_sid, content='I prefer cocoa without sugar.')
    target, args = jobs.pop()
    target(*args)
    assert dirty(w.ds, w.a)
    state = w.service.inventory(w.a)['rebuild']
    assert state['state'] == 'changed' and 'Rebuild again' in state['error']
