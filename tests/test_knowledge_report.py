"""Scoped reports inspect deterministic inputs, never ask a model or export settings."""
import json

import pytest

from core import profile, safety
from core.knowledge import can_recall, reference
from core.knowledge_report import build_report, render_markdown
from core.state_gate import STATE, install_state_gate
from test_companion_api import engine, tmp_store, world  # noqa: F401
from test_knowledge_controls import automatic, knowledge_world  # noqa: F401
from test_presence import presence_world  # noqa: F401


def report(w, owner=None):
    return build_report(w.service, w.a if owner is None else owner, 'Companion', generated_at='2026-01-01T00:00:00+00:00')


def entries(data):
    return {m['id']: m for m in data['memories']}


def test_report_is_owned_deterministic_and_has_no_model_or_profile_side_effects(knowledge_world, monkeypatch):
    w = knowledge_world
    mid, _ = automatic(w, 'I prefer jasmine tea.')
    w.service.remember(w.b, 'OTHER COMPANION SECRET')
    w.service.remember('', 'DEFAULT MEMORY SECRET')
    w.notes.store.write(w.a, about_you='- Jasmine tea', our_story='- A conversation')
    w.notes.store.write(w.b, about_you='OTHER NOTE SECRET')
    w.ds.save_message(w.b_sid, 'user', 'OTHER TRANSCRIPT SECRET')

    def forbidden(*args, **kwargs):
        raise AssertionError('A report must not infer, search vectors or read global personal/profile settings for an owned companion')

    monkeypatch.setattr(profile, 'load_user_profile', forbidden)
    monkeypatch.setattr(w.manager.store.collection, 'query', forbidden)
    monkeypatch.setattr(w.notes, '_call', forbidden)
    monkeypatch.setattr(w.ds, 'get_relationship_graph', forbidden)
    before = [(k.key, k.value) for k in w.ds.get_all_keys()]
    first, second = report(w), report(w)
    assert first == second
    assert first['schema_version'] == 1 and first['summary']['eligible_memories'] == 1
    assert first['memories'][0]['sources'][0]['message_id'] == mid
    encoded = json.dumps(first)
    assert all(secret not in encoded for secret in ('OTHER COMPANION SECRET', 'DEFAULT MEMORY SECRET', 'OTHER NOTE SECRET', 'OTHER TRANSCRIPT SECRET'))
    assert first['legacy_context'] == [] and 'metadata' not in first['memories'][0]
    assert [(k.key, k.value) for k in w.ds.get_all_keys()] == before


def test_report_eligibility_matches_recall_safety_and_dirty_state(knowledge_world, monkeypatch):
    w = knowledge_world
    current_mid, current_ids = automatic(w)
    added = w.service.remember(w.a, 'Added directly')
    legacy = w.manager.store.add_memory('Legacy fact', avatar_id=w.a)
    blocked = w.manager.store.add_memory('BLOCKED FACT', avatar_id=w.a, user_context='manual')
    changed_mid = w.ds.save_message(w.a_sid, 'user', 'Original source')
    changed = w.manager.store.add_memory('Extracted from old source', avatar_id=w.a, user_context='auto_extracted',
                                       source_refs=[reference(w.ds.get_message(changed_mid))])
    conn = w.ds._connect()
    try:
        conn.execute('UPDATE messages SET content=? WHERE id=?', ('Edited source', changed_mid))
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(safety, 'blocks_message', lambda content: content == 'BLOCKED FACT')
    data = entries(report(w))
    for memory in w.service._memories(w.a):
        assert data[memory['id']]['eligibility']['eligible'] == (can_recall(memory, w.ds) and memory['id'] != blocked)
    assert data[legacy]['eligibility']['reason_codes'] == ['source_unrecorded']
    assert data[blocked]['eligibility']['reason_codes'] == ['content_blocked']
    assert data[changed]['eligibility']['reason_codes'] == ['source_changed']
    assert data[changed]['sources'][0]['content'] == 'Edited source'
    assert data[changed]['sources'][0]['sha256'] != data[changed]['sources'][0]['recorded_sha256']
    assert data[added]['origin'] == 'user_added'
    # A boundary removes earlier history, while valid retained memories remain eligible.
    w.ds.set_key('knowledge_recall:' + w.a, {'through': current_mid})
    assert entries(report(w))[current_ids[0]]['eligibility']['eligible']
    w.notes.store.write(w.a, about_you='Saved derived text')
    w.ds.set_key('knowledge_dirty_owner:' + w.a, {'pending': True})
    paused = report(w)
    assert paused['recall']['repair_pending']
    assert all('repair_pending' in m['eligibility']['reason_codes'] for m in paused['memories'])
    assert all(not n['eligibility']['eligible'] for n in paused['notebook'])


@pytest.mark.parametrize('source_kind', ['foreign', 'deleted', 'invalid'])
def test_unavailable_sources_do_not_leak_foreign_excerpts(knowledge_world, source_kind):
    w = knowledge_world
    sid = w.b_sid if source_kind == 'foreign' else w.a_sid
    mid = w.ds.save_message(sid, 'user', 'SOURCE SECRET')
    refs = [reference(w.ds.get_message(mid))]
    if source_kind == 'invalid':
        refs[0]['sha256'] = 'bad'
    saved = w.manager.store.add_memory('A saved claim', avatar_id=w.a, user_context='auto_extracted', source_refs=refs)
    if source_kind == 'deleted':
        conn = w.ds._connect()
        try:
            conn.execute('DELETE FROM messages WHERE id=?', (mid,))
            conn.commit()
        finally:
            conn.close()
    item = entries(report(w))[saved]
    assert not item['eligibility']['eligible'] and 'source_unavailable' in item['eligibility']['reason_codes']
    assert 'SOURCE SECRET' not in json.dumps(item['sources'])


def test_sources_follow_same_owner_moves_and_preserve_assistant_source_role(knowledge_world):
    w = knowledge_world
    mid = w.ds.save_message(w.a_sid, 'assistant', 'An assistant statement, not a user assertion')
    saved = w.service.remember(w.a, 'The user explicitly saved this', source_id=mid)
    destination = w.ds.create_session('Moved source')
    w.ds.set_session_avatar(destination, w.a)
    w.ds.move_messages([w.a_sid], destination)
    item = entries(report(w))[saved]
    assert item['eligibility']['eligible']
    assert item['sources'][0]['session_id'] == destination and item['sources'][0]['role'] == 'assistant'
    assert any('Assistant messages are not user assertions' in text for text in report(w)['limitations'])


def test_notebook_is_current_aggregate_with_truthful_provenance(knowledge_world):
    w = knowledge_world
    w.notes.store.write(w.a, about_you='OLD NOTE HISTORY')
    w.notes.store.write(w.a, about_you='- Current exact notebook', our_story='- Our story')
    w.notes.store.update_state(w.a, source_kind='conversation', source_session_id=w.a_sid, source_after=4,
                               source_through=20, includes_user_edits=True, includes_legacy_notes=True)
    data = report(w)
    note = data['notebook'][0]
    assert note['content'] == '- Current exact notebook' and note['origin'] == 'derived_note'
    assert note['provenance'] == {'kind': 'derived_aggregate', 'claim_sources_available': False,
        'source_kind': 'conversation', 'includes_user_edits': True, 'includes_legacy_notes': True,
        'latest_update_range': {'session_id': w.a_sid, 'after': 4, 'through': 20}}
    assert 'OLD NOTE HISTORY' not in json.dumps(data)
    w.notes.store.update_state(w.a, source_session_id=w.b_sid)
    assert 'latest_update_range' not in report(w)['notebook'][0]['provenance']


def test_history_respects_owner_boundary_compaction_limits_and_current_mood(knowledge_world):
    w = knowledge_world
    ids = [w.ds.save_message(w.a_sid, 'user', f'line {i}', mood={'primary_emotion': 'hopeful'}) for i in range(12)]
    w.notes.config = {'companion': {'notes': {'history_messages': 6}}}
    w.notes.store.update_state(w.a, compacted_through=ids[-3])
    w.ds.set_key('knowledge_recall:' + w.a, {'through': ids[1]})
    data = report(w)['conversations'][0]
    assert [m['message_id'] for m in data['history_candidates']] == ids[-2:]
    assert data['mood_context']['content'] == 'hopeful' and data['mood_context']['source']['message_id'] == ids[-1]
    w.ds.set_key('knowledge_dirty_owner:' + w.a, {'pending': True})
    data = report(w)['conversations'][0]
    assert [m['message_id'] for m in data['history_candidates']] == ids[-6:]
    assert data['history_after'] == ids[1] and data['candidate_limit'] == 6


def test_history_uses_session_specific_runtime_policy(knowledge_world, monkeypatch):
    w = knowledge_world
    first = w.ds.save_message(w.a_sid, 'user', 'First chat history')
    second_sid = w.ds.create_session('Another owned chat')
    w.ds.set_session_avatar(second_sid, w.a)
    second = w.ds.save_message(second_sid, 'user', 'Second chat history')
    calls = []
    def history_after(owner, session_id):
        calls.append((owner, session_id))
        return first if session_id == w.a_sid else 0
    monkeypatch.setattr(w.notes, 'history_after', history_after)
    conversations = {c['session_id']: c for c in report(w)['conversations']}
    assert conversations[w.a_sid]['history_candidates'] == []
    assert conversations[second_sid]['history_candidates'][0]['message_id'] == second
    assert calls == [(w.a, w.a_sid), (w.a, second_sid)]


@pytest.mark.parametrize('compacted', [False, True])
def test_report_history_matches_chat_when_accepted_checkin_becomes_stale(knowledge_world, monkeypatch, compacted):
    from core.knowledge import prompt_messages
    from core.presence import PresenceService
    w = knowledge_world
    first = w.ds.save_message(w.a_sid, 'user', 'Synthetic starting point')
    for index in range(4):
        w.ds.save_message(w.a_sid, 'user', f'Synthetic line {index}')
    current = w.ds.save_message(w.a_sid, 'assistant', 'Current accepted check-in')
    stale = w.ds.save_message(w.a_sid, 'assistant', 'Invalidated check-in that chat must not recall')
    for index in range(4, 8):
        w.ds.save_message(w.a_sid, 'user', f'Synthetic line {index}')
    epoch = w.ds.dataset_epoch()['value']
    with w.ds._connect() as conn:
        conn.execute('INSERT INTO presence_inbox(id,job_id,avatar_id,session_id,dataset_epoch,kind,text,created_at,expires_at,snapshot,invalidated,accepted_message_id) '
                     'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                     ('synthetic-item', 'synthetic-job', w.a, w.a_sid, epoch,
                      'checkin', 'Invalidated check-in', 1, 2, '{}', 1, stale))
        conn.execute('INSERT INTO presence_inbox(id,job_id,avatar_id,session_id,dataset_epoch,kind,text,created_at,expires_at,snapshot,invalidated,accepted_message_id) '
                     'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                     ('synthetic-current', 'synthetic-current-job', w.a, w.a_sid, epoch,
                      'checkin', 'Current accepted check-in', 1, 2, 'valid', 0, current))
        conn.commit()
    w.notes.config = {'companion': {'notes': {'history_messages': 6}}}
    after = first if compacted else 0
    monkeypatch.setattr(w.notes, 'history_after', lambda _owner, _session: after)
    monkeypatch.setattr(PresenceService, '_snapshot_valid', lambda _self, raw, **_kwargs: raw == 'valid')
    records = w.ds.get_messages_after(w.a_sid, after) if after else w.ds.get_messages(w.a_sid, limit=6)
    expected = [m.id for m in prompt_messages(w.ds, w.a_sid, records)[-6:]]
    data = next(c for c in report(w)['conversations'] if c['session_id'] == w.a_sid)
    assert stale not in expected
    assert current in expected
    assert [m['message_id'] for m in data['history_candidates']] == expected


def test_reflection_summary_counts_only_eligible_sources(knowledge_world, monkeypatch):
    from core import reflection
    w = knowledge_world
    entry = {'id': 'synthetic-entry', 'text': 'Derived thought', 'note': 'about_you', 'confidence': .5,
             'user_edited': False, 'uncertainty': '', 'created_at': '', 'updated_at': '',
             'sources': [{'message_id': 999999, 'session_id': w.a_sid, 'sha256': 'a' * 64, 'start': 0, 'end': 1}]}
    monkeypatch.setattr(reflection, 'current_notebook', lambda *_args, **_kwargs: {'entries': [entry]})
    data = report(w)
    assert data['reflection_notebook'][0]['eligibility']['eligible'] is False
    assert data['summary']['eligible_reflection_entries'] == 0


def test_report_does_not_recover_unrelated_presence_jobs(knowledge_world, monkeypatch):
    from core.presence import PresenceService
    w = knowledge_world
    w.ds.save_message(w.a_sid, 'user', 'Synthetic conversation')
    accepted = w.ds.save_message(w.a_sid, 'assistant', 'Current synthetic check-in')
    epoch = w.ds.dataset_epoch()['value']
    with w.ds._connect() as conn:
        conn.execute('INSERT INTO presence_jobs(id,avatar_id,session_id,dataset_epoch,kind,occurrence,due_at,expires_at,state,process_id,snapshot,created_at,updated_at) '
                     'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                     ('synthetic-running-job', w.a, w.a_sid, epoch, 'checkin', 'synthetic',
                      1, 2, 'running', 'previous-process', '{}', 1, 1))
        conn.execute('INSERT INTO presence_inbox(id,job_id,avatar_id,session_id,dataset_epoch,kind,text,created_at,expires_at,snapshot,invalidated,accepted_message_id) '
                     'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                     ('synthetic-current', 'synthetic-current-job', w.a, w.a_sid, epoch, 'checkin',
                      'Current synthetic check-in', 1, 2, 'valid', 0, accepted))
        conn.commit()
    assert not hasattr(w.ds, '_presence_service')
    validations = []
    monkeypatch.setattr(PresenceService, '_snapshot_valid', lambda _self, raw, **_kwargs: validations.append(raw) or True)
    data = report(w)
    assert 'valid' in validations and accepted in [m['message_id'] for m in data['conversations'][0]['history_candidates']]
    with w.ds._connect() as conn:
        assert conn.execute('SELECT state FROM presence_jobs WHERE id=?', ('synthetic-running-job',)).fetchone()[0] == 'running'
    assert not hasattr(w.ds, '_presence_service')


def test_report_tracks_actual_checkin_validity_after_source_edit(presence_world):
    from types import SimpleNamespace
    from core.knowledge import KnowledgeService
    from test_presence import enable, publish
    w = presence_world
    enable(w)
    item = publish(w)
    accepted = w.service.action(item['id'], 'accept', w.sid, w.epoch)['accepted_message_id']
    manager = SimpleNamespace(store=SimpleNamespace(get_all=lambda **_kwargs: []))
    service = KnowledgeService(w.ds, manager)

    def candidates():
        data = build_report(service, '', 'Default', generated_at='2026-01-01T00:00:00+00:00')
        return [m['message_id'] for m in data['conversations'][0]['history_candidates']]

    assert accepted in candidates()
    w.ds.change_message(w.user, w.sid, content='A corrected synthetic source')
    assert w.ds.get_message(accepted).content == item['text']  # Visible history is preserved.
    assert accepted not in candidates()


def test_default_report_whitelists_only_actual_legacy_prompt_context_and_floor_suppresses_it(knowledge_world, monkeypatch):
    w = knowledge_world
    monkeypatch.setattr(profile, 'load_user_profile', lambda: {'name': 'PROFILE NAME SECRET', 'facts': ['FACT SECRET'],
        'mood_history': ['calm'], 'config': {'api_key': 'SETTINGS SECRET'}})
    w.ds.set_humor_style({'style': 'dry', 'level': 'gentle', 'api_key': 'HUMOR EXTRA SECRET'})
    w.ds.set_jokes_and_references({'recurring_topics': {'tea': {'text': 'tea breaks', 'mention_count': 3}},
                                 'other': 'JOKE EXTRA SECRET'})
    w.service.remember('', 'Default fact')
    w.service.remember(w.a, 'OTHER COMPANION SECRET')
    data = report(w, '')
    assert data['scope']['id'] == '_default' and not data['notebook']
    assert {c['id'] for c in data['legacy_context']} == {'mood', 'callback-1', 'humor'}
    assert all(c['eligibility']['eligible'] for c in data['legacy_context'])
    assert all(secret not in json.dumps(data) for secret in ('PROFILE NAME SECRET', 'FACT SECRET', 'SETTINGS SECRET',
        'HUMOR EXTRA SECRET', 'JOKE EXTRA SECRET', 'OTHER COMPANION SECRET'))
    w.ds.set_key('knowledge_recall:@default', {'through': 1})
    assert all(c['eligibility']['reason_codes'] == ['legacy_recall_boundary'] for c in report(w, '')['legacy_context'])


def test_report_routes_exports_are_no_store_scope_checked_and_read_only(knowledge_world):
    w = knowledge_world
    saved = w.service.remember(w.a, 'Verbatim Unicode: café ☕')
    path = f'/api/knowledge/scopes/{w.a}/report'
    view = w.client.get(path)
    downloaded = w.client.get(path + '/export?format=json')
    markdown = w.client.get(path + '/export?format=markdown')
    assert view.status_code == downloaded.status_code == markdown.status_code == 200
    assert {k: v for k, v in view.json.items() if k != 'generated_at'} == {k: v for k, v in downloaded.json.items() if k != 'generated_at'}
    assert entries(view.json)[saved]['content'] == 'Verbatim Unicode: café ☕'
    assert 'Verbatim Unicode: café ☕' in markdown.get_data(as_text=True)
    for response in (view, downloaded, markdown):
        assert response.headers['Cache-Control'] == 'no-store'
    assert downloaded.headers['Content-Disposition'] == 'attachment; filename="altera-companion-knowledge.json"'
    assert markdown.mimetype == 'text/markdown'
    for url, status in (('/api/knowledge/scopes/missing/report', 404), (path + '/export?format=html', 400)):
        response = w.client.get(url)
        assert response.status_code == status and response.headers['Cache-Control'] == 'no-store'
    assert w.client.post(path, json={}).status_code == 405
    # Pure reads retain existing local read semantics; origin protection remains for mutations.
    assert w.client.get(path, headers={'Origin': 'http://evil.example'}).status_code == 200
    assert w.client.post(f'/api/knowledge/scopes/{w.a}/memories', json={'content': 'x'},
                         headers={'Origin': 'http://evil.example'}).status_code == 403


def test_snapshot_gate_accepts_own_request_lease_but_refuses_writers(knowledge_world):
    w = knowledge_world
    install_state_gate(w.client.application)
    path = f'/api/knowledge/scopes/{w.a}/report'
    assert w.client.get(path).status_code == 200
    lease = STATE.enter('altera-notes')
    try:
        response = w.client.get(path)
        assert response.status_code == 409 and response.headers['Cache-Control'] == 'no-store'
        assert not STATE.maintenance
    finally:
        lease.release()
    assert w.client.get(path + '/export?format=json').status_code == 200
    assert not STATE.active and not STATE.maintenance


def test_report_inherits_remote_access_restrictions(knowledge_world):
    from core.remote_access import install_remote_access
    w = knowledge_world
    install_remote_access(w.client.application)
    path = f'/api/knowledge/scopes/{w.a}/report'
    assert w.client.get(path, environ_base={'REMOTE_ADDR': '127.0.0.1'}).status_code == 200
    for suffix in ('', '/export?format=json', '/export?format=markdown'):
        response = w.client.get(path + suffix, environ_base={'REMOTE_ADDR': '192.0.2.10'})
        assert response.status_code == 403 and response.json['code'] == 'remote_access_denied'
        assert response.headers['Cache-Control'] == 'no-store'


def test_companion_named_shared_and_default_reports_remain_distinct(knowledge_world, world):
    from test_companion_images import make_forge
    w = knowledge_world
    make_forge(world.forge, npc_id='shared', first='Shared')
    world.avatars.import_from_forge('shared')
    separate = w.service.remember('shared', 'Named Shared fact')
    default = w.service.remember('', 'Default-only fact')
    assert set(entries(w.client.get('/api/knowledge/scopes/shared/report').json)) == {separate}
    assert set(entries(w.client.get('/api/knowledge/scopes/_default/report').json)) == {default}


def test_markdown_preserves_fields_without_rendering_saved_html_or_links(knowledge_world):
    w = knowledge_world
    text = '<script>alert(1)</script> [click](https://example.invalid)\n**bold** café'
    saved = w.service.remember(w.a, text)
    data = report(w)
    markdown = render_markdown(data)
    assert entries(data)[saved]['content'] == text
    assert '<script>' not in markdown and '&lt;script&gt;' in markdown
    assert '\\[click\\]' in markdown and '\\*\\*bold\\*\\* café' in markdown
    assert 'Eligible: yes' in markdown and 'user_added' in markdown
    assert 'Current notebook' in markdown and 'Conversation context candidates' in markdown
