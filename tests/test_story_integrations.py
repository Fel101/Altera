"""Whole seams: actual chat, factual evidence, restore and notebook inspection."""
from datetime import datetime, UTC
import json
from types import SimpleNamespace as NS

import pytest

from core import companion
from core.episodes import EpisodeError, get_episode_service, invalidate_all
from core.knowledge import floor
from core.knowledge_report import build_report, render_markdown
from core.reflection import ReflectionService
from core.state_gate import STATE
from test_backups import backup_world  # noqa: F401
from test_companion_api import engine, tmp_store, world  # noqa: F401
from test_knowledge_controls import knowledge_world  # noqa: F401
from test_polish_reliability import decoded
from test_reflection import enable, output, publish

NOW = datetime(2026, 10, 28, 12, tzinfo=UTC)
PROVIDER = {'provider_key': 'local', 'base_url': 'http://127.0.0.1:1/v1', 'model': 'fake'}


def reflection(ds, sid, monkeypatch, *, episodes=False):
    service = ReflectionService(ds, clock=lambda: NOW, provider_getter=lambda: dict(PROVIDER), llm=lambda *a, **kw: output(episodes=episodes))
    ds._reflection_service = service
    monkeypatch.setattr(service, '_busy', lambda: False)
    wrapped = NS(ds=ds, sid=sid, service=service, epoch=ds.dataset_epoch()['value'])
    enable(wrapped); publish(wrapped)
    return wrapped


def test_optional_photo_failure_never_changes_an_owned_chat_to_default_context(knowledge_world, monkeypatch):
    w = knowledge_world
    w.service.remember(w.a, 'Owner likes jasmine tea')
    w.service.remember('', 'DEFAULT_PRIVATE_SECRET')
    w.ds.save_message(w.a_sid, 'user', 'What about jasmine tea?')
    def failed(*a, **kw): raise ConnectionError('Synthetic optional photo failure')
    monkeypatch.setattr(companion, 'photos_ready', failed)
    ctx = w.engine.build_context(w.a_sid)
    assert ctx.avatar_id == w.a and ctx.avatar['id'] == w.a
    assert 'DEFAULT_PRIVATE_SECRET' not in ctx.system_prompt and 'Owner likes jasmine tea' in ctx.system_prompt
    assert not ctx.photos_enabled


def test_actual_chat_uses_and_accounts_reserved_recall_only_after_saved_reply(knowledge_world):
    w = knowledge_world; epoch = w.ds.dataset_epoch()['value']
    user = w.ds.save_message(w.a_sid, 'user', 'I enjoyed jasmine tea by the window.')
    assistant = w.ds.save_message(w.a_sid, 'assistant', 'That sounds peaceful.')
    episodes = get_episode_service(w.ds)
    item = episodes.capture(w.a_sid, epoch, {'request_id': 'source_story', 'title': 'Jasmine tea',
        'kind': 'actual_exchange', 'sources': [{'message_id': user}, {'message_id': assistant}], 'in_story': True})['episode']
    events = decoded(w.client.post('/api/chat/stream', json={'session_id': w.a_sid, 'dataset_epoch': epoch,
        'request_id': 'story_chat_saved', 'message': 'Tell me about that jasmine tea by the window.'}))
    assert events[-1]['status'] == 'saved'
    assert '## Optional sourced callback' in w.engine.llm.calls[-1][0]['content']
    with w.ds._connect() as conn:
        used = conn.execute('SELECT episode_ids FROM episode_recall_turns WHERE assistant_message_id=?', (events[-1]['message_id'],)).fetchone()
        assert json.loads(used[0]) == [item['id']]
        assert conn.execute('SELECT count(*) FROM episode_recall_reservations').fetchone()[0] == 0
    assert episodes.inventory(w.a_sid)['recall_budget']['daily_used'] == 1


def test_failed_actual_reply_releases_recall_without_spending_budget(knowledge_world):
    w = knowledge_world; epoch = w.ds.dataset_epoch()['value']
    user = w.ds.save_message(w.a_sid, 'user', 'Jasmine tea by the window')
    get_episode_service(w.ds).capture(w.a_sid, epoch, {'request_id': 'prior_story', 'kind': 'actual_exchange',
        'title': 'Jasmine tea', 'sources': [{'message_id': user}]})
    def broken():
        yield 'A partial reply'
        raise ConnectionError('Synthetic model failure')
    w.engine.llm.chunks = broken()
    events = decoded(w.client.post('/api/chat/stream', json={'session_id': w.a_sid, 'dataset_epoch': epoch,
        'request_id': 'story_chat_failure', 'message': 'Jasmine tea by the window?'}))
    assert events[-1]['status'] == 'failed'
    with w.ds._connect() as conn:
        assert conn.execute('SELECT count(*) FROM episode_recall_reservations').fetchone()[0] == 0
        assert conn.execute('SELECT count(*) FROM episode_recall_turns').fetchone()[0] == 0


def test_reflection_notebook_is_visible_in_prompt_and_snapshot_without_overwriting_manual_notes(knowledge_world, monkeypatch):
    w = knowledge_world
    w.notes.store.write(w.a, about_you='MANUAL_NOTE_RETAINED')
    w.ds.save_message(w.a_sid, 'user', 'I enjoy jasmine tea.')
    r = reflection(w.ds, w.a_sid, monkeypatch)
    assert w.notes.store.read(w.a)['about_you'] == 'MANUAL_NOTE_RETAINED'
    ctx = w.engine.build_context(w.a_sid)
    assert 'Derived reflection notebook' in ctx.system_prompt and 'they enjoy jasmine tea' in ctx.system_prompt
    assert 'MANUAL_NOTE_RETAINED' in ctx.system_prompt
    with STATE.exclusive():
        report = build_report(w.service, w.a, 'Companion')
    assert report['summary']['eligible_reflection_entries'] == 1
    entry = report['reflection_notebook'][0]
    assert entry['origin'] == 'derived_reflection' and entry['sources'][0]['role'] == 'user'
    assert entry['sources'][0]['content'] == 'I enjoy jasmine tea.'
    assert entry['sources'][0]['sha256'] in render_markdown(report)
    w.service.remember(w.b, 'OTHER_COMPANION_SECRET')
    with STATE.exclusive(): other = build_report(w.service, w.b, 'Other')
    assert not other['reflection_notebook']
    assert r.service.current_notebook(w.a)['entries']


def test_normal_notebook_cannot_launder_assistant_echo_of_a_reflection(knowledge_world, monkeypatch):
    w = knowledge_world
    w.ds.save_message(w.a_sid, 'user', 'I enjoy jasmine tea.')
    r = reflection(w.ds, w.a_sid, monkeypatch)
    w.ds.save_message(w.a_sid, 'assistant', 'DERIVED_ASSISTANT_ECHO_INVENTED_PARIS')
    assert w.notes.run_now(w.a, w.a_sid)['ok']
    assert 'DERIVED_ASSISTANT_ECHO_INVENTED_PARIS' not in json.dumps(w.calls[-1])
    entry = r.service.current_notebook(w.a)['entries'][0]
    r.service.edit_notebook(entry['id'], w.a_sid, r.epoch, entry['revision'], {'dismiss': True}, avatar_id=w.a)
    assert not r.service.current_notebook(w.a)['entries']
    assert 'DERIVED_ASSISTANT_ECHO_INVENTED_PARIS' not in json.dumps(w.notes.store.read(w.a))


def test_explicit_forget_scrubs_derived_notebook_copies_and_cancels_episodes(knowledge_world, monkeypatch):
    w = knowledge_world; w.ds.save_message(w.a_sid, 'user', 'I enjoy jasmine tea.')
    r = reflection(w.ds, w.a_sid, monkeypatch, episodes=True)
    item = w.service.remember(w.a, 'A user saved preference')
    assert r.service.current_notebook(w.a)['entries']
    w.service.change(w.a, item, forget=True)
    assert floor(w.ds, w.a) > 0 and not r.service.current_notebook(w.a)['entries']
    with w.ds._connect() as conn:
        assert conn.execute('SELECT text FROM reflection_notebook').fetchone()[0] == ''
        assert conn.execute('SELECT count(*) FROM reflection_reports').fetchone()[0] == 0
    assert not get_episode_service(w.ds).preview_recall(w.a_sid, 'jasmine tea')['episodes']


def test_pin_alone_does_not_promote_generated_episode_to_independent_recall(knowledge_world, monkeypatch):
    w = knowledge_world; w.ds.save_message(w.a_sid, 'user', 'I enjoy jasmine tea.')
    reflection(w.ds, w.a_sid, monkeypatch, episodes=True)
    service = get_episode_service(w.ds); epoch = w.ds.dataset_epoch()['value']
    item = service.inventory(w.a_sid)['episodes'][0]
    assert not item['eligible'] and item['curation'] == 'reflection'
    pinned = service.change(item['id'], w.a_sid, epoch, item['revision'], {'pinned': True})['episode']
    assert pinned['pinned'] and not pinned['eligible'] and pinned['curation'] == 'reflection'
    curated = service.change(item['id'], w.a_sid, epoch, pinned['revision'], {'in_story': True})['episode']
    assert curated['eligible'] and curated['curation'] == 'manual_curated' and curated['kind'] == 'derived_summary'


def test_story_page_and_scope_lookup_are_read_only(knowledge_world, monkeypatch):
    w = knowledge_world; w.ds.dataset_epoch()
    def forbidden(*a, **kw): raise AssertionError('A story read must not migrate, open a chat or infer')
    from core import companions
    monkeypatch.setattr(companions, 'migrate_legacy', forbidden)
    before = len(w.ds.list_sessions())
    response = w.client.get('/story')
    assert response.status_code == 200 and response.headers['Cache-Control'] == 'no-store'
    assert 'js/story.js' in response.text
    scopes = w.client.get('/api/knowledge/scopes').json['scopes']
    assert next(s for s in scopes if s['id'] == w.a)['session_id'] == w.a_sid
    assert len(w.ds.list_sessions()) == before and not w.calls


def test_restored_story_requires_review_but_retains_pins_and_user_curation(backup_world):
    b = backup_world
    service = get_episode_service(b.store); epoch = b.store.dataset_epoch()['value']
    row = service.capture(b.sid, epoch, {'request_id': 'backup_story', 'kind': 'actual_exchange',
        'title': 'The first hello', 'summary': 'A saved greeting', 'sources': [{'message_id': 1}, {'message_id': 2}], 'in_story': True})['episode']
    row = service.change(row['id'], b.sid, epoch, row['revision'], {'pinned': True, 'salience': 5})['episode']
    saved = b.service.create(); preview = b.service.preview(b.service.archive_path(saved['id'])); b.service.apply(preview['token'])
    current_epoch = b.store.dataset_epoch()['value']
    historical = service.inventory(b.sid)['episodes'][0]
    assert historical['status'] == 'stale' and not historical['title'] and historical['pinned']
    review = service.review_restored(row['id'], b.sid, current_epoch, row['revision'])
    assert review['episode']['title'] == 'The first hello' and review['sources'][0]['excerpt'] == 'Hello'
    restored = service.revalidate_restored(row['id'], b.sid, current_epoch, row['revision'], review['reviewed_sources'])['episode']
    assert restored['in_story'] and restored['pinned'] and restored['salience'] == 5 and restored['status'] == 'current'
    assert service.revalidate_restored(row['id'], b.sid, current_epoch, row['revision'], review['reviewed_sources'])['episode']['id'] == row['id']


def test_restored_review_cannot_bypass_an_edited_source_or_forget_boundary(knowledge_world):
    w = knowledge_world; epoch = w.ds.dataset_epoch()['value']
    user = w.ds.save_message(w.a_sid, 'user', 'Original source')
    service = get_episode_service(w.ds)
    row = service.capture(w.a_sid, epoch, {'request_id': 'restore_source', 'kind': 'actual_exchange',
        'title': 'Original', 'sources': [{'message_id': user}]})['episode']
    w.ds.set_key('dataset_epoch', {'value': 'restored'})
    with w.ds._connect() as conn: invalidate_all(conn); conn.commit()
    review = service.review_restored(row['id'], w.a_sid, 'restored', row['revision'])
    w.ds.change_message(user, w.a_sid, content='Corrected source')
    with pytest.raises(EpisodeError): service.revalidate_restored(row['id'], w.a_sid, 'restored', row['revision'], review['reviewed_sources'])
    assert service.inventory(w.a_sid)['episodes'][0]['status'] == 'stale'


def test_restore_review_routes_require_exact_owner_and_reviewed_source_ranges(knowledge_world):
    w = knowledge_world; epoch = w.ds.dataset_epoch()['value']
    from core.routes.episodes import bp
    w.client.application.register_blueprint(bp)
    user = w.ds.save_message(w.a_sid, 'user', 'A retained source for review.')
    service = get_episode_service(w.ds)
    row = service.capture(w.a_sid, epoch, {'request_id': 'route_restore', 'kind': 'actual_exchange',
        'title': 'Retained curation', 'sources': [{'message_id': user}]})['episode']
    w.ds.set_key('dataset_epoch', {'value': 'restored'})
    with w.ds._connect() as conn: invalidate_all(conn); conn.commit()
    scope = {'session_id': w.a_sid, 'dataset_epoch': 'restored', 'revision': row['revision']}
    route = '/api/episodes/' + row['id']
    assert w.client.get(route + '/restore-review', query_string=scope).status_code == 400
    assert w.client.get(route + '/restore-review', query_string={**scope, 'avatar_id': w.b}).status_code == 409
    scope['avatar_id'] = w.a
    review = w.client.get(route + '/restore-review', query_string=scope)
    assert review.status_code == 200 and review.json['sources'][0]['excerpt'] == 'A retained source for review.'
    refs = review.json['reviewed_sources']
    bad = [{**refs[0], 'end': refs[0]['end'] - 1}]
    payload = {**scope, 'reviewed_sources': bad, 'confirm': 'restore-curation'}
    assert w.client.post(route + '/revalidate', json=payload).status_code == 409
    assert service.inventory(w.a_sid)['episodes'][0]['status'] == 'stale'
    payload['reviewed_sources'] = refs
    saved = w.client.post(route + '/revalidate', json=payload)
    assert saved.status_code == 200 and saved.json['episode']['status'] == 'current'
    retry = w.client.post(route + '/revalidate', json=payload)
    assert retry.json == saved.json


def test_old_epoch_curation_can_be_forgotten_without_reactivating_it(knowledge_world):
    w = knowledge_world; epoch = w.ds.dataset_epoch()['value']
    user = w.ds.save_message(w.a_sid, 'user', 'A source I later forget.')
    service = get_episode_service(w.ds)
    row = service.capture(w.a_sid, epoch, {'request_id': 'old_forget', 'kind': 'actual_exchange',
        'title': 'OLD_RETAINED_CURATION', 'sources': [{'message_id': user}]})['episode']
    w.ds.set_key('dataset_epoch', {'value': 'restored'})
    with w.ds._connect() as conn: invalidate_all(conn); conn.commit()
    result = service.forget(row['id'], w.a_sid, 'restored', row['revision'], owner=w.a)
    assert result['episode']['status'] == 'forgotten' and floor(w.ds, w.a) >= user
    with w.ds._connect() as conn:
        stored = conn.execute('SELECT title,summary,source_refs FROM companion_episodes WHERE id=?', (row['id'],)).fetchone()
    assert list(stored) == ['', '', '[]']
    with pytest.raises(EpisodeError): service.review_restored(row['id'], w.a_sid, 'restored', row['revision'], owner=w.a)


@pytest.mark.parametrize('removal', ['companion', 'session'])
def test_removal_cleans_cold_reflection_and_episode_stores_without_new_service_leases(knowledge_world, monkeypatch, removal):
    """Lifecycle cleanup also works before a reflection worker is initialized."""
    from core import companions
    from core.knowledge import reference
    from core.reflection import DEFAULTS
    w = knowledge_world
    epoch = w.ds.dataset_epoch()['value']
    episodes = get_episode_service(w.ds)
    records = {}
    for label, owner, sid in [('remove', w.a, w.a_sid), ('keep', w.b, w.b_sid)]:
        user = w.ds.save_message(sid, 'user', f'{label}: original synthetic tea source.')
        assistant = w.ds.save_message(sid, 'assistant', 'A saved synthetic reply.')
        item = episodes.capture(sid, epoch, {'request_id': 'lifecycle_' + label, 'kind': 'actual_exchange',
            'title': label + ' source', 'sources': [{'message_id': user}]})['episode']
        refs = json.dumps([{**reference(w.ds.get_message(user)), 'role': 'user', 'start': 0, 'end': 10}])
        snapshot = json.dumps({'avatar_id': owner, 'session_id': sid, 'sources': json.loads(refs)})
        job, report, note = 'rj_' + label, 'rr_' + label, 'rn_' + label
        with w.ds._connect() as conn:
            conn.execute('INSERT INTO reflection_preferences VALUES(?,?,?,1,0,?,0,0,0)', (owner, sid, epoch, json.dumps(DEFAULTS)))
            conn.execute('INSERT INTO reflection_jobs(id,avatar_id,session_id,dataset_epoch,occurrence,due_at,expires_at,state,snapshot,created_at,updated_at) VALUES(?,?,?,?,?,0,9999999999,?,?,0,0)',
                         (job, owner, sid, epoch, 'lifecycle', 'pending', snapshot))
            conn.execute('INSERT INTO reflection_notebook(id,avatar_id,session_id,dataset_epoch,job_id,note,text,uncertainty,confidence,sources,snapshot,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,0,0)',
                         (note, owner, sid, epoch, job, 'about_you', label + ' private derived note', '', .7, refs, snapshot))
            conn.execute('INSERT INTO reflection_reports VALUES(?,?,?,?,?,?,?,?,?,0,0)',
                         (report, job, owner, sid, epoch, label + ' private report', snapshot, json.dumps([item['id']]), json.dumps([note])))
            conn.execute('INSERT INTO episode_recall_turns VALUES(?,?,?,?,?,?)', (user, assistant, owner, epoch, json.dumps([item['id']]), NOW.isoformat()))
            conn.execute('INSERT INTO episode_recall_reservations VALUES(?,?,?,?,?,?,?)',
                         ('orphan_' + label, owner, sid, epoch, 'retired_synthetic_process', json.dumps([item['id']]), 'synthetic_digest'))
            conn.commit()
        records[label] = {'episode': item['id'], 'job': job, 'note': note, 'report': report, 'user': user}
    assert not hasattr(w.ds, '_reflection_service')
    def forbidden_initialization(*args, **kwargs):
        raise AssertionError('Deletion must not initialize reflection or acquire a nested state lease')
    monkeypatch.setattr(ReflectionService, '__init__', forbidden_initialization)
    if removal == 'companion':
        companions.remove(w.a, memory_manager=w.manager)
        assert w.notes.store.dir(w.a) is None
    else:
        w.ds.delete_session(w.a_sid)
    assert w.ds.get_session(w.a_sid) is None and w.ds.get_session(w.b_sid) is not None
    assert w.ds.get_message(records['keep']['user']) is not None
    with w.ds._connect() as conn:
        for table, field in [('companion_episodes', 'owner'), ('episode_recall_turns', 'owner'),
                             ('episode_recall_reservations', 'owner'), ('reflection_notebook', 'avatar_id'),
                             ('reflection_reports', 'avatar_id'), ('reflection_preferences', 'avatar_id')]:
            assert conn.execute(f'SELECT count(*) FROM {table} WHERE {field}=?', (w.a,)).fetchone()[0] == 0, table
            assert conn.execute(f'SELECT count(*) FROM {table} WHERE {field}=?', (w.b,)).fetchone()[0] == 1, table
        removed_job = conn.execute('SELECT state,snapshot FROM reflection_jobs WHERE id=?', (records['remove']['job'],)).fetchone()
        assert tuple(removed_job) == ('stale', '{}')
        kept_job = conn.execute('SELECT state FROM reflection_jobs WHERE id=?', (records['keep']['job'],)).fetchone()
        assert kept_job[0] == 'pending'
        assert conn.execute('SELECT text FROM reflection_notebook WHERE id=?', (records['keep']['note'],)).fetchone()[0] == 'keep private derived note'
    assert STATE.maintenance is False
