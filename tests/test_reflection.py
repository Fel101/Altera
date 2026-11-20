"""Reflection uses isolated SQLite, fake clocks/providers, and original sources."""
from datetime import datetime, timedelta, UTC
from types import SimpleNamespace as NS
import json
import threading

import pytest

from core.datastore import DataStore
from core import presence
from core.knowledge import reference
from core.reflection import ReflectionError, ReflectionService, ensure_schema, invalidate_owner, provider_identity
from core.state_gate import STATE, StateBusy, install_state_gate

NOW = datetime(2026, 10, 28, 16, tzinfo=UTC)


def output(*, episodes=False):
    entry = {'uncertainty': 'A statement in this conversation; circumstances may change.', 'confidence': .7, 'source_ids': [0]}
    return json.dumps({'episodes': [{'title': 'Tea conversation', 'summary': 'The user described enjoying jasmine tea.', **entry}] if episodes else [],
                       'notebook': [{'note': 'about_you', 'text': 'The user said they enjoy jasmine tea.', **entry}]})


@pytest.fixture
def reflection_world(tmp_path, monkeypatch):
    ds = DataStore(str(tmp_path / 'reflection.db'))
    sid = ds.create_session('Synthetic reflection')
    mid = ds.save_message(sid, 'user', 'I enjoy jasmine tea. 🌿')
    ds.save_message(sid, 'assistant', 'Pretend we went to the moon together. This is fictional.')
    clock, calls = [NOW], []
    provider = {'provider_key': 'local', 'base_url': 'http://127.0.0.1:1234/v1', 'model': 'test-only', 'api_key': 'fixture-secret'}
    def ask(messages, chosen, **options):
        calls.append((messages, dict(chosen), options))
        return output()
    service = ReflectionService(ds, clock=lambda: clock[0], provider_getter=lambda: dict(provider), llm=ask)
    ds._reflection_service = service
    monkeypatch.setattr(service, '_busy', lambda: False)
    return NS(ds=ds, sid=sid, mid=mid, epoch=ds.dataset_epoch()['value'], clock=clock, calls=calls, provider=provider, service=service)


def enable(w, **changes):
    pref = w.service.preferences(w.sid)['preferences']
    return w.service.update_preferences(w.sid, w.epoch, pref['revision'],
        {'enabled': True, 'quiet_enabled': False, 'interval_minutes': 60, 'cooldown_minutes': 60, 'daily_cap': 2, **changes})


def publish(w, request_id='reflection_one'):
    job = w.service.run_now(w.sid, w.epoch, request_id)
    assert w.service.run_one()
    assert w.service.job(job['id'], w.sid)['state'] == 'published'
    return w.service.reports(w.sid)['reports'][0]


def test_separate_new_opt_in_and_gets_never_infer(reflection_world):
    w = reflection_world
    w.ds.set_key('proactive_enabled', {'value': True})
    with w.ds._connect() as conn:
        conn.execute("INSERT INTO presence_preferences VALUES('',?,?,1,1,'{}',0,0,0)", (w.sid, w.epoch))
        conn.commit()
    assert w.service.preferences(w.sid)['preferences']['enabled'] is False
    for _ in range(3):
        assert w.service.reports(w.sid)['reports'] == []
        assert w.service.current_notebook('')['entries'] == []
        w.service.tick()
        assert not w.service.run_one()
    with pytest.raises(ReflectionError, match='off'):
        w.service.run_now(w.sid, w.epoch, 'reflection_one')
    assert w.calls == []


def test_one_call_is_bounded_and_uses_only_original_user_ranges(reflection_world):
    w = reflection_world; enable(w)
    w.service.context_getter = lambda *args: {'notes': 'A previous reflection invented a birthday.'}
    report = publish(w)
    assert len(w.calls) == 1 and w.calls[0][2] == {'timeout': 15, 'max_retries': 0, 'max_tokens': 1200}
    system, prompt = w.calls[0][0]
    assert 'not evidence' in system['content'] and 'No tools' in system['content']
    assert 'jasmine tea' in prompt['content']
    assert 'moon' not in prompt['content'] and 'birthday' not in prompt['content']
    source = report['sources'][0]
    assert source == {**reference(w.ds.get_message(w.mid)), 'role': 'user', 'start': 0,
                      'end': len(w.ds.get_message(w.mid).content), 'status': 'current'}
    note = w.service.current_notebook('')
    assert 'jasmine tea' in note['about_you'] and 'Uncertainty:' in note['about_you']
    assert note['derived'] and note['entries'][0]['confidence'] == .7
    assert w.ds.get_message_count(w.sid) == 2


def test_source_window_is_bounded_and_long_unicode_ranges_are_exact(reflection_world):
    w = reflection_world; enable(w)
    for index in range(15):
        w.ds.save_message(w.sid, 'user', f'{index}: ' + '🌿' * 1500)
    publish(w)
    sources = json.loads(w.calls[0][0][1]['content'])['original_user_sources']
    assert len(sources) == 8 and all(len(item['text']) == 1000 for item in sources)
    for item in sources:
        message = w.ds.get_message(item['source']['message_id'])
        assert item['source']['sha256'] == reference(message)['sha256']
        assert message.content[item['source']['start']:item['source']['end']] == item['text']


def test_run_request_retries_resolve_original_even_after_disable(reflection_world):
    w = reflection_world; enable(w)
    first = w.service.run_now(w.sid, w.epoch, 'reflection_one')
    assert w.service.run_now(w.sid, w.epoch, 'reflection_one')['id'] == first['id']
    with pytest.raises(ReflectionError, match='already'):
        w.service.run_now(w.sid, w.epoch, 'reflection_two')
    assert w.service.run_one()
    pref = w.service._pref(w.sid)
    w.service.update_preferences(w.sid, w.epoch, pref['revision'], {'enabled': False})
    assert w.service.run_now(w.sid, w.epoch, 'reflection_one')['id'] == first['id']
    assert len(w.calls) == 1


def test_remote_approval_is_reflection_specific_exact_and_contains_no_secret(reflection_world):
    w = reflection_world
    w.provider['base_url'] = 'https://example.invalid/v1'
    approval = provider_identity(w.provider)['fingerprint']
    presence_approval = presence.provider_identity(w.provider)['fingerprint']
    assert approval != presence_approval
    with pytest.raises(ReflectionError, match='approve'):
        enable(w)
    with pytest.raises(ReflectionError, match='reflection provider'):
        enable(w, provider_approval=presence_approval)
    enable(w, provider_approval=approval)
    assert w.service.preferences(w.sid)['provider']['approved']
    assert w.provider['api_key'] not in json.dumps(w.service.preferences(w.sid))
    for field, value in [('model', 'other'), ('api_key', 'other'), ('base_url', 'https://other.invalid/v1')]:
        original = w.provider[field]
        w.provider[field] = value
        assert not w.service.preferences(w.sid)['provider']['approved']
        with pytest.raises(ReflectionError, match='approved'):
            w.service.run_now(w.sid, w.epoch, 'reflection_' + field)
        w.provider[field] = original


def test_off_switch_works_with_a_broken_provider(reflection_world):
    w = reflection_world; enable(w)
    w.provider['base_url'] = 'broken'
    assert w.service.preferences(w.sid)['provider']['error']
    pref = w.service._pref(w.sid)
    assert not w.service.update_preferences(w.sid, w.epoch, pref['revision'],
        {'enabled': False, 'provider_approval': None})['preferences']['enabled']


@pytest.mark.parametrize('change', ['edit', 'delete', 'floor', 'dirty', 'revision', 'epoch', 'provider', 'model', 'credentials', 'disable', 'new_source', 'quiet', 'expiry'])
def test_mutation_during_call_cannot_publish(reflection_world, change):
    w = reflection_world; enable(w)
    w.service.run_now(w.sid, w.epoch, 'reflection_one')
    original = w.service.llm
    def mutated(*args, **kwargs):
        raw = original(*args, **kwargs)
        if change == 'edit': w.ds.change_message(w.mid, w.sid, content='I prefer coffee now.')
        elif change == 'delete': w.ds.change_message(w.mid, w.sid, delete=True)
        elif change == 'floor': w.ds.set_key('knowledge_recall:@default', {'through': w.mid})
        elif change == 'dirty': w.ds.set_key('knowledge_dirty_owner:@default', {'pending': True})
        elif change == 'revision': w.ds.set_key(f'knowledge_dirty:{w.sid}', {'revision': 7})
        elif change == 'epoch': w.ds.set_key('dataset_epoch', {'value': 'replacement'})
        elif change == 'provider': w.provider['base_url'] = 'https://example.invalid/v1'
        elif change == 'model': w.provider['model'] = 'other'
        elif change == 'credentials': w.provider['api_key'] = 'other'
        elif change == 'new_source': w.ds.save_message(w.sid, 'user', 'A new correction arrived.')
        elif change == 'expiry': w.clock[0] += timedelta(hours=3)
        else:
            pref = w.service._pref(w.sid)
            w.service.update_preferences(w.sid, w.epoch, pref['revision'],
                {'enabled': False} if change == 'disable' else {'quiet_enabled': True, 'quiet_start': 0, 'quiet_end': 0})
        return raw
    w.service.llm = mutated
    assert w.service.run_one() and len(w.calls) == 1
    assert w.service.reports(w.sid)['reports'] == []
    assert w.service.current_notebook('')['entries'] == []
    assert not w.service.run_one() and len(w.calls) == 1


def test_notebook_edits_and_dismissal_survive_repeat_source_reflection(reflection_world):
    w = reflection_world; enable(w)
    report = publish(w)
    first = report['notebook_proposals'][0]
    edited = w.service.edit_notebook(first['id'], w.sid, w.epoch, first['revision'],
        {'text': 'My correction: tea is an occasional preference.', 'uncertainty': 'User corrected.'})
    assert edited['user_edited'] and edited['sources'] == first['sources']
    with pytest.raises(ReflectionError, match='revision'):
        w.service.edit_notebook(first['id'], w.sid, w.epoch, first['revision'], {'text': 'Lost old revision'})
    w.clock[0] += timedelta(minutes=61)
    second = publish(w, 'reflection_two')['notebook_proposals'][0]
    assert second['id'] == first['id'] and second['text'] == edited['text'] and second['revision'] == edited['revision']
    dismissed = w.service.edit_notebook(second['id'], w.sid, w.epoch, second['revision'], {'dismiss': True})
    assert dismissed['state'] == 'dismissed'
    w.clock[0] += timedelta(days=1)
    publish(w, 'reflection_three')
    assert w.service.current_notebook('')['entries'] == []
    # Neither old generated addenda nor a correction become fresh LLM evidence.
    assert all('My correction' not in call[0][1]['content'] for call in w.calls)


def test_changed_source_forget_and_restore_hide_derived_notebook(reflection_world):
    w = reflection_world; enable(w); publish(w)
    assert w.service.current_notebook('')['entries']
    w.ds.set_key('knowledge_recall:@default', {'through': w.mid})
    assert w.service.current_notebook('')['entries'] == []
    assert w.service.reports(w.sid)['reports'][0]['notebook_proposals'] == []
    w.ds.set_key('knowledge_recall:@default', {'through': 0})
    w.ds.set_key('dataset_epoch', {'value': 'restored'})
    assert w.service.current_notebook('')['entries'] == []
    pref = w.service.preferences(w.sid)['preferences']
    assert not pref['enabled'] and pref['needs_review']


def test_no_eligible_original_sources_fail_closed(reflection_world):
    w = reflection_world; enable(w)
    w.ds.set_key('knowledge_recall:@default', {'through': w.mid})
    with pytest.raises(ReflectionError, match='No eligible'):
        w.service.run_now(w.sid, w.epoch, 'reflection_one')
    assert w.calls == []


def test_planning_is_leased_bounded_catchup_and_expires_old_jobs(reflection_world):
    w = reflection_world; enable(w)
    baseline = dict(STATE.active)
    w.clock[0] += timedelta(days=30)
    with STATE.exclusive():
        with pytest.raises(StateBusy): w.service.tick()
    w.service.tick(); w.service.tick()
    assert STATE.active == baseline
    with w.ds._connect() as conn:
        assert conn.execute('SELECT count(*) FROM reflection_jobs').fetchone()[0] == 1
    w.clock[0] += timedelta(hours=3)
    assert not w.service.run_one() and not w.calls
    assert w.service.reports(w.sid)['jobs'][0]['state'] == 'expired'


def test_daily_cap_and_cooldown_count_failed_inference_too(reflection_world):
    w = reflection_world; enable(w, daily_cap=1)
    def failed(*args, **kwargs):
        w.calls.append('failed')
        raise RuntimeError('Private endpoint details must not be exposed')
    w.service.llm = failed
    job = w.service.run_now(w.sid, w.epoch, 'reflection_one')
    assert w.service.run_one()
    failed_job = w.service.job(job['id'], w.sid)
    assert failed_job['state'] == 'failed' and 'Private endpoint' not in failed_job['error']
    w.clock[0] += timedelta(hours=2)
    with pytest.raises(ReflectionError, match='daily'):
        w.service.run_now(w.sid, w.epoch, 'reflection_two')
    assert not w.service.run_one() and len(w.calls) == 1


def test_quiet_hours_use_timezone_and_apply_before_generation(reflection_world):
    w = reflection_world
    enable(w, timezone='America/New_York', quiet_enabled=True, quiet_start=11 * 60, quiet_end=13 * 60)
    with pytest.raises(ReflectionError, match='quiet'):
        w.service.run_now(w.sid, w.epoch, 'reflection_one')
    w.clock[0] += timedelta(hours=2)
    publish(w)


def test_restart_failure_is_durable_and_not_retried(reflection_world):
    w = reflection_world; enable(w)
    job = w.service.run_now(w.sid, w.epoch, 'reflection_one')
    with w.ds._connect() as conn:
        conn.execute("UPDATE reflection_jobs SET state='running',process_id='old-process',attempts=1,started_at=?", (NOW.timestamp(),))
        conn.commit()
    restarted = ReflectionService(w.ds, clock=lambda: w.clock[0], provider_getter=lambda: w.provider, llm=w.service.llm)
    assert restarted.job(job['id'], w.sid)['state'] == 'failed'
    assert not restarted.run_one() and w.calls == []


def test_two_workers_and_presence_share_one_global_slot(reflection_world):
    w = reflection_world; enable(w)
    w.service.run_now(w.sid, w.epoch, 'reflection_one')
    assert presence._generation_lock.acquire(blocking=False)
    try:
        assert not w.service.run_one() and w.calls == []
    finally:
        presence._generation_lock.release()
    entered, release = threading.Event(), threading.Event()
    original = w.service.llm
    def blocked(*args, **kwargs):
        entered.set(); assert release.wait(10)
        return original(*args, **kwargs)
    w.service.llm = blocked
    worker = threading.Thread(target=w.service.run_one); worker.start()
    try:
        assert entered.wait(10)
        competing = ReflectionService(w.ds, clock=lambda: w.clock[0], provider_getter=lambda: w.provider, llm=original)
        assert not competing.run_one()
        assert 'altera-reflection' in STATE.active.values()
    finally:
        release.set(); worker.join(10)
    assert len(w.calls) == 1 and len(w.service.reports(w.sid)['reports']) == 1
    assert 'altera-reflection' not in STATE.active.values()


@pytest.mark.parametrize('bad', ['broken', '{}', '{"sources":[]}'])
def test_malformed_restored_job_is_stale_without_inference(reflection_world, bad):
    w = reflection_world; enable(w)
    job = w.service.run_now(w.sid, w.epoch, 'reflection_one')
    with w.ds._connect() as conn:
        conn.execute('UPDATE reflection_jobs SET snapshot=?', (bad,)); conn.commit()
    assert w.service.run_one() and w.calls == []
    assert w.service.job(job['id'], w.sid)['state'] == 'stale'


@pytest.mark.parametrize('bad', ['not json', '{"episodes":[],"notebook":[],"tool_calls":[]}',
    '{"episodes":[],"notebook":[{"note":"about_you","text":"Tea","uncertainty":"","confidence":NaN,"source_ids":[0]}]}',
    '{"episodes":[],"notebook":[{"note":"about_you","text":"Tea","uncertainty":"","confidence":0.7,"source_ids":[999]}]}'])
def test_invalid_output_fails_once_without_publish(reflection_world, bad):
    w = reflection_world; enable(w)
    w.service.llm = lambda *args, **kwargs: bad
    job = w.service.run_now(w.sid, w.epoch, 'reflection_one')
    assert w.service.run_one() and not w.service.run_one()
    assert w.service.job(job['id'], w.sid)['state'] == 'failed'
    assert w.service.reports(w.sid)['reports'] == []


def test_schema_creation_preserves_caller_transaction(reflection_world):
    w = reflection_world
    with w.ds._connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute("INSERT INTO user_profile(key,value) VALUES('reflection_test_rollback','{}')")
        ensure_schema(conn)
        conn.rollback()
    assert w.ds.get_key('reflection_test_rollback') is None


def test_episode_and_notebook_publication_are_one_atomic_transaction(reflection_world, monkeypatch):
    w = reflection_world; enable(w)
    w.service.llm = lambda *args, **kwargs: output(episodes=True)
    from core.episodes import get_episode_service
    episodes = get_episode_service(w.ds)
    original = w.service._publish
    def fail_after_publish(conn, *args):
        assert conn.in_transaction
        original(conn, *args)
        assert conn.execute('SELECT count(*) FROM companion_episodes').fetchone()[0] == 1
        raise RuntimeError('Synthetic commit failure')
    monkeypatch.setattr(w.service, '_publish', fail_after_publish)
    job = w.service.run_now(w.sid, w.epoch, 'reflection_one')
    assert w.service.run_one()
    assert w.service.job(job['id'], w.sid)['state'] == 'failed'
    with w.ds._connect() as conn:
        for table in ('reflection_notebook', 'reflection_reports', 'companion_episodes'):
            assert conn.execute('SELECT count(*) FROM ' + table).fetchone()[0] == 0
    monkeypatch.setattr(w.service, '_publish', original)
    w.clock[0] += timedelta(hours=2)
    report = publish(w, 'reflection_two')
    assert len(report['episode_ids']) == 1
    episode = episodes.inventory(w.sid)['episodes'][0]
    assert episode['kind'] == 'derived_summary' and episode['curation'] == 'reflection'
    assert not episode['in_story'] and not episode['pinned']
    w.clock[0] += timedelta(days=1)
    varied = json.loads(output(episodes=True))
    varied['episodes'][0]['summary'] = 'Fresh generated wording of the same source.'
    w.service.llm = lambda *args, **kwargs: json.dumps(varied)
    again = publish(w, 'reflection_three')
    assert again['episode_ids'] == report['episode_ids']
    assert len(episodes.inventory(w.sid)['episodes']) == 1


def test_database_presence_claim_prevents_reflection_claim(reflection_world):
    w = reflection_world; enable(w)
    job = w.service.run_now(w.sid, w.epoch, 'reflection_one')
    with w.ds._connect() as conn:
        conn.execute("INSERT INTO presence_jobs(id,avatar_id,session_id,dataset_epoch,kind,occurrence,due_at,expires_at,state,snapshot,created_at,updated_at) "
                     "VALUES('synthetic_presence','',?,?,'checkin','manual:synthetic',0,9999999999,'running','{}',0,0)", (w.sid, w.epoch))
        conn.commit()
    assert not w.service.run_one() and w.calls == []
    assert w.service.job(job['id'], w.sid)['state'] == 'pending'


def test_malformed_notebook_provenance_fails_closed_without_public_data_leak(reflection_world):
    w = reflection_world; enable(w); publish(w)
    with w.ds._connect() as conn:
        conn.execute("UPDATE reflection_notebook SET sources='[1,2,3]'"); conn.commit()
    assert w.service.current_notebook('')['entries'] == []
    report = w.service.reports(w.sid)['reports'][0]
    assert report['notebook_proposals'][0]['state'] == 'stale'
    assert report['notebook_proposals'][0]['sources'] == []
    assert report['notebook_proposals'][0]['text'] == ''


def test_malformed_restored_preferences_are_off_and_repairable(reflection_world):
    w = reflection_world; enable(w)
    with w.ds._connect() as conn:
        conn.execute("UPDATE reflection_preferences SET settings='broken'"); conn.commit()
    pref = w.service.preferences(w.sid)['preferences']
    assert pref['needs_review'] and not pref['enabled'] and pref['timezone'] == 'UTC'
    repaired = w.service.update_preferences(w.sid, w.epoch, pref['revision'], {'enabled': False, 'provider_approval': None})
    assert not repaired['preferences']['needs_review'] and not repaired['preferences']['enabled']


def test_fault_backup_and_source_invalidation_fail_closed(reflection_world, monkeypatch):
    w = reflection_world; enable(w); publish(w)
    real_busy = ReflectionService._busy.__get__(w.service)
    monkeypatch.setattr(w.service, '_busy', real_busy)
    try:
        STATE.fail_closed('Synthetic locked storage')
        assert not w.service.run_one()
        assert w.service.current_notebook('')['entries'] == []
        with pytest.raises(ReflectionError, match='recovery'):
            w.service.reports(w.sid)
        with pytest.raises(StateBusy):
            w.service.tick()
    finally:
        STATE.recovered()
    with w.ds._connect() as conn:
        invalidate_owner(conn, '', 'Forget'); conn.commit()
    assert w.service.current_notebook('')['entries'] == []
    w.service.clear_owner('')
    with w.ds._connect() as conn:
        assert conn.execute('SELECT count(*) FROM reflection_notebook').fetchone()[0] == 0
        assert conn.execute('SELECT count(*) FROM reflection_reports').fetchone()[0] == 0
        assert conn.execute('SELECT snapshot FROM reflection_jobs').fetchone()[0] == '{}'


def test_http_origin_frozen_owner_epoch_validation_and_read_side_effects(reflection_world, monkeypatch):
    from flask import Flask
    from core import datastore
    from core.routes.reflection import bp
    from core.request_security import install_request_security
    w = reflection_world
    monkeypatch.setattr(datastore, '_datastore_instance', w.ds)
    app = Flask(__name__); app.register_blueprint(bp)
    install_request_security(app); install_state_gate(app)
    client = app.test_client()
    response = client.get('/api/reflection/preferences', query_string={'session_id': w.sid})
    assert response.status_code == 200 and response.headers['Cache-Control'] == 'no-store'
    data = {'avatar_id': '', 'session_id': w.sid, 'dataset_epoch': w.epoch, 'revision': 0,
            'settings': {'enabled': True, 'quiet_enabled': False}}
    assert client.patch('/api/reflection/preferences', json=data, headers={'Origin': 'https://evil.invalid'}).status_code == 403
    assert client.patch('/api/reflection/preferences', json={**data, 'avatar_id': 'another'}).status_code == 409
    assert client.patch('/api/reflection/preferences', json={**data, 'dataset_epoch': 'old'}).status_code == 409
    assert client.patch('/api/reflection/preferences', json={k: v for k, v in data.items() if k != 'avatar_id'}).status_code == 400
    assert client.patch('/api/reflection/preferences', json=data).status_code == 200
    assert client.get('/api/reflection/reports', query_string={'session_id': w.sid}).status_code == 200
    run = {key: data[key] for key in ('avatar_id', 'session_id', 'dataset_epoch')}
    run['request_id'] = 'reflection_one'
    queued = client.post('/api/reflection/run', json=run)
    assert queued.status_code == 202 and queued.json['job']['avatar_id'] == '' and w.calls == []
    assert client.get('/api/reflection/reports', query_string={'session_id': w.sid}).json['reports'] == []
    assert w.service.run_one()
    entry = w.service.current_notebook('')['entries'][0]
    edited = client.patch('/api/reflection/notebook/' + entry['id'], json={**run, 'revision': entry['revision'], 'text': 'Corrected'})
    assert edited.status_code == 400  # request_id belongs only to run, never silently ignored.
    del run['request_id']
    edited = client.patch('/api/reflection/notebook/' + entry['id'], json={**run, 'revision': entry['revision'], 'text': 'Corrected'})
    assert edited.status_code == 200 and edited.json['entry']['text'] == 'Corrected'
    assert len(w.calls) == 1


@pytest.mark.parametrize('status', ['sending', 'streaming'])
def test_notebook_correction_waits_for_same_owner_durable_reply(reflection_world, status):
    from core.turns import PROCESS_ID
    w = reflection_world
    enable(w)
    publish(w)
    entry = w.service.current_notebook('')['entries'][0]
    with w.ds._connect() as conn:
        conn.execute('INSERT INTO chat_turns(request_id,session_id,fingerprint,process_id,status,user_message_id) VALUES(?,?,?,?,?,?)',
                     ('synthetic_active_correction', w.sid, 'fake', PROCESS_ID, status, w.mid))
        conn.commit()
    with pytest.raises(ReflectionError) as caught:
        w.service.edit_notebook(entry['id'], w.sid, w.epoch, entry['revision'], {'text': 'A corrected derived note.'}, avatar_id='')
    assert caught.value.status == 409
    current = w.service.current_notebook('')['entries'][0]
    assert current['text'] == entry['text'] and current['revision'] == entry['revision']


def test_notebook_correction_waits_for_saved_text_with_native_reply_lease(reflection_world):
    """Saving text does not release a native speech writer's existing lease."""
    w = reflection_world
    enable(w)
    publish(w)
    entry = w.service.current_notebook('')['entries'][0]
    lease = STATE.enter('altera-reply')
    try:
        with pytest.raises(ReflectionError) as caught:
            w.service.edit_notebook(entry['id'], w.sid, w.epoch, entry['revision'], {'text': 'A corrected derived note.'}, avatar_id='')
        assert caught.value.status == 409
    finally:
        lease.release()
    current = w.service.current_notebook('')['entries'][0]
    assert current['text'] == entry['text'] and current['revision'] == entry['revision']
