"""Durable presence uses fake clocks/providers and never contacts a real model."""
from datetime import datetime, timedelta, UTC
from types import SimpleNamespace as NS
import json
import threading

import pytest

from core import config
from core.app import create_app
from core.datastore import DataStore
from core.knowledge import prompt_messages, reference
from core.presence import PresenceError, PresenceService, provider_identity
from core.state_gate import STATE, install_state_gate
from test_backups import backup_world  # noqa: F401
from test_knowledge_controls import knowledge_world  # noqa: F401
from test_companion_api import engine, tmp_store, world  # noqa: F401

NOW = datetime(2026, 10, 27, 16, tzinfo=UTC)


@pytest.fixture
def presence_world(tmp_path, monkeypatch):
    ds = DataStore(str(tmp_path / 'presence.db'))
    sid = ds.create_session('Synthetic presence')
    user = ds.save_message(sid, 'user', 'I enjoy jasmine tea.')
    epoch = ds.dataset_epoch()['value']
    with ds._connect() as conn:
        conn.execute("UPDATE messages SET timestamp='2026-10-26 10:00:00'")
        conn.commit()
    clock, calls = [NOW], []
    provider = {'provider_key': 'local', 'base_url': 'http://127.0.0.1:1234/v1', 'model': 'test-only', 'api_key': 'fixture-key'}
    def context(session, owner, boundary):
        messages = [m for m in ds.get_messages(session) if m.role == 'user' and m.id > boundary]
        return {'personality': config.get_personality(), 'avatar': None, 'notes': {}, 'memories': [],
                'sources': [reference(m) for m in messages[-6:]], 'conversation': '\n'.join(m.content for m in messages[-6:])}
    def ask(messages, chosen, **options):
        calls.append((messages, dict(chosen), options))
        return 'A quiet hello. How is your afternoon going?'
    service = PresenceService(ds, clock=lambda: clock[0], provider_getter=lambda: dict(provider), context_getter=context, llm=ask)
    ds._presence_service = service
    # Most unit tests isolate job behavior; gate/HTTP tests below use the real busy check.
    monkeypatch.setattr(service, '_busy', lambda: False)
    return NS(ds=ds, sid=sid, user=user, epoch=epoch, clock=clock, calls=calls, provider=provider, service=service)


def enable(w, **changes):
    pref = w.service.preferences(w.sid)['preferences']
    return w.service.update_preferences(w.sid, w.epoch, pref['revision'],
        {'enabled': True, 'quiet_enabled': False, 'cooldown_minutes': 15, 'daily_cap': 5, **changes})


def publish(w, request_id='request_one'):
    w.service.check_now(w.sid, w.epoch, request_id)
    assert w.service.run_one()
    data = w.service.inbox(w.sid)
    assert data['items'] and data['items'][0]['state'] == 'available'
    return data['items'][0]


def test_presence_is_new_opt_in_and_reads_have_no_inference_side_effects(presence_world):
    w = presence_world
    w.ds.set_key('proactive_enabled', {'value': True})
    assert not w.service.preferences(w.sid)['preferences']['enabled']
    for _ in range(3):
        assert w.service.inbox(w.sid)['items'] == []
        w.service.tick()
        assert not w.service.run_one()
    with pytest.raises(PresenceError, match='off'):
        w.service.check_now(w.sid, w.epoch, 'request_one')
    assert w.calls == []


def test_queue_is_idempotent_and_bounded_to_one_active_job(presence_world):
    w = presence_world; enable(w)
    first = w.service.check_now(w.sid, w.epoch, 'request_one')
    assert w.service.check_now(w.sid, w.epoch, 'request_one')['id'] == first['id']
    with pytest.raises(PresenceError, match='already'):
        w.service.check_now(w.sid, w.epoch, 'request_two')
    assert w.service.run_one() and not w.service.run_one()
    assert len(w.calls) == 1
    options = w.calls[0][2]
    assert options == {'timeout': 15, 'max_retries': 0, 'max_tokens': 256}
    assert 'jasmine tea' in w.calls[0][0][1]['content']
    assert 'No tools' in w.calls[0][0][0]['content']


def test_lost_check_response_resolves_original_after_publication_or_disable(presence_world):
    w = presence_world; enable(w)
    first = w.service.check_now(w.sid, w.epoch, 'request_one')
    assert w.service.run_one()
    assert w.service.check_now(w.sid, w.epoch, 'request_one')['id'] == first['id']
    pref = w.service._pref(w.sid)
    w.service.update_preferences(w.sid, w.epoch, pref['revision'], {'enabled': False})
    retry = w.service.check_now(w.sid, w.epoch, 'request_one')
    assert retry['id'] == first['id'] and retry['state'] == 'stale' and len(w.calls) == 1
    with w.ds._connect() as conn:
        assert conn.execute('SELECT count(*) FROM presence_jobs').fetchone()[0] == 1


def test_read_dismiss_and_atomic_accept_are_distinct_and_retry_safe(presence_world):
    w = presence_world; enable(w); item = publish(w)
    assert w.ds.get_message_count(w.sid) == 1
    read = w.service.action(item['id'], 'read', w.sid, w.epoch)
    assert read['read_at'] and not read['accepted_message_id']
    assert w.ds.get_message_count(w.sid) == 1
    results, errors = [], []
    def accept():
        try:
            results.append(w.service.action(item['id'], 'accept', w.sid, w.epoch))
        except Exception as error:
            errors.append(error)
    workers = [threading.Thread(target=accept) for _ in range(2)]
    for worker in workers: worker.start()
    for worker in workers: worker.join(10)
    assert not errors and len(results) == 2 and len({r['accepted_message_id'] for r in results}) == 1
    assert w.ds.get_message_count(w.sid) == 2 and len(w.calls) == 1
    assert results[0]['accepted_message_id'] in [m.id for m in prompt_messages(w.ds, w.sid, w.ds.get_messages(w.sid))]
    w.clock[0] += timedelta(minutes=16)
    second = publish(w, 'request_two')
    dismissed = w.service.action(second['id'], 'dismiss', w.sid, w.epoch)
    assert dismissed['state'] == 'dismissed' and dismissed['text'] == ''
    with pytest.raises(PresenceError): w.service.action(second['id'], 'accept', w.sid, w.epoch)


@pytest.mark.parametrize('mutation', ['edit', 'delete', 'floor', 'epoch', 'provider', 'model', 'credentials', 'disabled', 'notes'])
def test_changes_during_inference_cannot_publish(presence_world, mutation):
    w = presence_world; enable(w)
    w.service.check_now(w.sid, w.epoch, 'request_one')
    original = w.service.llm
    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        if mutation == 'edit': w.ds.change_message(w.user, w.sid, content='I prefer coffee now.')
        elif mutation == 'delete': w.ds.change_message(w.user, w.sid, delete=True)
        elif mutation == 'floor': w.ds.set_key('knowledge_recall:@default', {'through': w.user})
        elif mutation == 'epoch': w.ds.set_key('dataset_epoch', {'value': 'replacement'})
        elif mutation == 'provider': w.provider['base_url'] = 'https://example.invalid/v1'
        elif mutation == 'model': w.provider['model'] = 'different'
        elif mutation == 'credentials': w.provider['api_key'] = 'changed'
        elif mutation == 'disabled':
            pref = w.service._pref(w.sid)
            w.service.update_preferences(w.sid, w.epoch, pref['revision'], {'enabled': False})
        else:
            old = w.service.context_getter
            w.service.context_getter = lambda *a: {**old(*a), 'notes': {'about_you': 'Changed'}}
        return result
    w.service.llm = changed
    assert w.service.run_one()
    assert w.service.inbox(w.sid)['items'] == [] and len(w.calls) == 1


def test_accepted_derived_history_is_filtered_after_source_edit_and_after_moving_sessions(presence_world):
    w = presence_world; enable(w); item = publish(w)
    accepted = w.service.action(item['id'], 'accept', w.sid, w.epoch)['accepted_message_id']
    w.ds.change_message(w.user, w.sid, content='Corrected source')
    assert accepted not in [m.id for m in prompt_messages(w.ds, w.sid, w.ds.get_messages(w.sid))]
    destination = w.ds.create_session('Split destination')
    with w.ds._connect() as conn:
        conn.execute('UPDATE messages SET session_id=? WHERE id=?', (destination, accepted)); conn.commit()
    assert w.ds.get_message(accepted).content == item['text']
    assert accepted not in [m.id for m in prompt_messages(w.ds, destination, w.ds.get_messages(destination))]
    assert w.service.inbox(w.sid)['items'][0]['text'] == ''


def test_expiry_caps_and_quiet_hours_apply_before_and_after_generation(presence_world):
    w = presence_world; enable(w, daily_cap=1)
    publish(w)
    w.clock[0] += timedelta(minutes=16)
    with pytest.raises(PresenceError, match='daily'):
        w.service.check_now(w.sid, w.epoch, 'request_two')
    w.clock[0] += timedelta(days=1)
    enable(w, quiet_enabled=True, timezone='America/New_York', quiet_start=0, quiet_end=1439)
    with pytest.raises(PresenceError, match='quiet'):
        w.service.check_now(w.sid, w.epoch, 'request_three')
    enable(w, quiet_enabled=False, expiry_minutes=15)
    job = w.service.check_now(w.sid, w.epoch, 'request_four')
    w.clock[0] += timedelta(minutes=16)
    assert not w.service.run_one()
    assert w.service.job(job['id'], w.sid)['state'] == 'expired'


def test_remote_consent_is_bound_to_exact_provider_model_and_credentials(presence_world):
    w = presence_world
    w.provider['base_url'] = 'https://example.invalid/v1'
    with pytest.raises(PresenceError, match='approve'): enable(w)
    fingerprint = provider_identity(w.provider)['fingerprint']
    assert w.provider['api_key'] not in json.dumps(w.service.preferences(w.sid))
    enable(w, provider_approval=fingerprint)
    assert w.service.preferences(w.sid)['provider']['approved']
    w.provider['model'] = 'different'
    assert not w.service.preferences(w.sid)['provider']['approved']
    with pytest.raises(PresenceError): w.service.check_now(w.sid, w.epoch, 'request_one')


def test_off_switch_remains_available_when_connection_is_broken(presence_world):
    w = presence_world; enable(w)
    w.provider['base_url'] = 'not-a-url'
    assert w.service.preferences(w.sid)['provider']['error']
    pref = w.service._pref(w.sid)
    assert not w.service.update_preferences(w.sid, w.epoch, pref['revision'], {'enabled': False, 'provider_approval': None})['preferences']['enabled']


def test_restart_reconciles_claim_once_without_automatically_retrying_inference(presence_world):
    w = presence_world; enable(w)
    job = w.service.check_now(w.sid, w.epoch, 'request_one')
    with w.ds._connect() as conn:
        conn.execute("UPDATE presence_jobs SET state='running',process_id='old-process',attempts=1"); conn.commit()
    restored = PresenceService(w.ds, clock=lambda: w.clock[0], provider_getter=lambda: dict(w.provider), context_getter=w.service.context_getter, llm=w.service.llm)
    assert restored.job(job['id'], w.sid)['state'] == 'failed'
    assert not restored.run_one() and w.calls == []


def test_schedule_catch_up_is_bounded_and_skips_recent_conversation(presence_world):
    w = presence_world; enable(w, interval_minutes=60)
    w.clock[0] += timedelta(days=30)
    w.service.tick(); w.service.tick()
    with w.ds._connect() as conn:
        assert conn.execute('SELECT count(*) FROM presence_jobs').fetchone()[0] == 1
    assert w.service.run_one() and len(w.calls) == 1
    w.clock[0] += timedelta(days=1)
    new = w.ds.save_message(w.sid, 'user', 'Just returned')
    with w.ds._connect() as conn:
        conn.execute('UPDATE messages SET timestamp=? WHERE id=?', (w.clock[0].isoformat(), new)); conn.commit()
    w.service.tick()
    assert not w.service.run_one() and len(w.calls) == 1


def test_malformed_restored_snapshot_and_deleted_session_fail_closed(presence_world):
    w = presence_world; enable(w); item = publish(w)
    accepted = w.service.action(item['id'], 'accept', w.sid, w.epoch)['accepted_message_id']
    with w.ds._connect() as conn:
        conn.execute("UPDATE presence_inbox SET snapshot='broken'"); conn.commit()
    assert w.service.inbox(w.sid)['items'][0]['state'] == 'stale'
    assert accepted not in [m.id for m in prompt_messages(w.ds, w.sid, w.ds.get_messages(w.sid))]
    w.ds.delete_session(w.sid)
    with w.ds._connect() as conn:
        row = conn.execute('SELECT text,snapshot,invalidated FROM presence_inbox').fetchone()
        assert tuple(row) == ('', '{}', 1)
        assert conn.execute('SELECT count(*) FROM presence_preferences').fetchone()[0] == 0


def test_http_reads_are_private_and_mutations_require_origin_and_epoch(presence_world, monkeypatch):
    w = presence_world
    from core import datastore
    monkeypatch.setattr(datastore, '_datastore_instance', w.ds)
    app = create_app(autostart=False); install_state_gate(app)
    client = app.test_client()
    response = client.get(f'/api/presence/inbox?session_id={w.sid}')
    assert response.status_code == 200 and response.headers['Cache-Control'] == 'no-store'
    assert w.calls == []
    body = {'session_id': w.sid, 'avatar_id': '', 'dataset_epoch': w.epoch, 'revision': 0, 'settings': {'enabled': True, 'quiet_enabled': False}}
    assert client.patch('/api/presence/preferences', json=body, headers={'Origin': 'https://evil.invalid'}).status_code == 403
    assert client.patch('/api/presence/preferences', json={**body, 'dataset_epoch': 'old'}).status_code == 409
    assert client.patch('/api/presence/preferences', json=body).status_code == 200
    assert client.get('/api/presence/inbox?session_id=999999999999999999999999').status_code == 400
    assert client.post('/api/presence/check-now', json={'session_id': True, 'dataset_epoch': w.epoch, 'request_id': 'request_one'}).status_code == 400
    assert w.calls == []


def test_real_busy_check_excludes_own_request_but_respects_other_work(presence_world):
    w = presence_world
    real = PresenceService._busy.__get__(w.service)
    lease = STATE.enter('altera-reply')
    try: assert real()
    finally: lease.release()
    assert not real()


@pytest.mark.parametrize('label', ['altera-presence-cycle', 'altera-reflection-cycle', 'altera-presence-plan', 'altera-reflection-plan'])
def test_planning_remains_owned_without_pretending_to_be_active_generation(presence_world, label):
    w = presence_world
    lease = STATE.enter(label)
    try:
        assert not PresenceService._busy(w.service)
        assert lease.key in STATE.active
    finally:
        lease.release()


@pytest.mark.parametrize('mutation', ['owner', 'epoch'])
def test_presence_rechecks_frozen_scope_inside_the_actual_preference_transaction(presence_world, monkeypatch, mutation):
    w = presence_world
    original = w.service._pref
    def changed(session_id):
        pref = original(session_id)
        with w.ds._connect() as conn:
            if mutation == 'owner':
                conn.execute('UPDATE sessions SET avatar_id=? WHERE id=?', ('different-companion', session_id))
            else:
                conn.execute("UPDATE user_profile SET value=? WHERE key='dataset_epoch'", (json.dumps({'value': 'new-dataset'}),))
            conn.commit()
        return pref
    monkeypatch.setattr(w.service, '_pref', changed)
    with pytest.raises(PresenceError, match='conversation or dataset changed'):
        w.service.update_preferences(w.sid, w.epoch, 0, {'enabled': False}, avatar_id='')
    with w.ds._connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM presence_preferences').fetchone()[0] == 0
    assert not STATE.active and w.calls == []


def test_schedule_planning_cannot_write_during_exclusive_backup_or_restore(presence_world):
    from core.state_gate import StateBusy
    w = presence_world; enable(w)
    w.clock[0] += timedelta(days=1)
    before = dict(STATE.active)
    with STATE.exclusive():
        with pytest.raises(StateBusy): w.service.tick()
    with w.ds._connect() as conn:
        assert conn.execute('SELECT count(*) FROM presence_jobs').fetchone()[0] == 0
    w.service.tick()
    assert STATE.active == before
    with w.ds._connect() as conn:
        assert conn.execute('SELECT count(*) FROM presence_jobs').fetchone()[0] == 1


def test_recent_goodnight_catches_up_once_across_midnight(presence_world):
    w = presence_world; enable(w)
    w.service.ritual(w.sid, w.epoch, {'kind': 'goodnight', 'minute': 23 * 60 + 45, 'enabled': True})
    w.clock[0] = datetime(2026, 10, 28, 0, 5, tzinfo=UTC)
    w.service.tick(); w.service.tick()
    assert w.service.run_one()
    assert w.service.inbox(w.sid)['items'][0]['kind'] == 'goodnight'
    w.clock[0] += timedelta(minutes=16)
    w.service.tick(); assert not w.service.run_one()


def test_two_workers_do_not_generate_the_same_job(presence_world):
    w = presence_world; enable(w)
    w.service.check_now(w.sid, w.epoch, 'request_one')
    entered, release = threading.Event(), threading.Event()
    original = w.service.llm
    def blocked(*a, **kw):
        entered.set(); assert release.wait(10)
        return original(*a, **kw)
    w.service.llm = blocked
    worker = threading.Thread(target=w.service.run_one); worker.start()
    try:
        assert entered.wait(10)
        competing = PresenceService(w.ds, clock=lambda: w.clock[0], provider_getter=lambda: dict(w.provider), context_getter=w.service.context_getter, llm=original)
        assert not competing.run_one()
    finally:
        release.set(); worker.join(10)
    assert len(w.calls) == 1 and len(w.service.inbox(w.sid)['items']) == 1


def test_ritual_revision_disable_and_daily_occurrence_are_durable(presence_world):
    w = presence_world; enable(w)
    ritual = w.service.ritual(w.sid, w.epoch, {'kind': 'greeting', 'minute': 15 * 60, 'enabled': True})
    w.service.tick(); w.service.tick()
    assert w.service.run_one() and not w.service.run_one()
    assert w.service.inbox(w.sid)['items'][0]['kind'] == 'greeting'
    w.clock[0] += timedelta(minutes=16)
    w.service.tick(); assert not w.service.run_one()
    changed = w.service.ritual(w.sid, w.epoch, {'enabled': False}, ritual['id'], ritual['revision'])
    assert not changed['enabled']
    with pytest.raises(PresenceError): w.service.ritual(w.sid, w.epoch, {'enabled': True}, ritual['id'], ritual['revision'])
    assert w.service.inbox(w.sid)['items'][0]['state'] == 'stale'


def test_accept_transaction_rolls_back_when_link_write_fails(presence_world):
    w = presence_world; enable(w); item = publish(w)
    with w.ds._connect() as conn:
        conn.execute("CREATE TRIGGER reject_accept BEFORE UPDATE OF accepted_message_id ON presence_inbox BEGIN SELECT RAISE(ABORT,'fixture link failure'); END"); conn.commit()
    with pytest.raises(Exception, match='fixture link failure'):
        w.service.action(item['id'], 'accept', w.sid, w.epoch)
    assert w.ds.get_message_count(w.sid) == 1
    with w.ds._connect() as conn:
        conn.execute('DROP TRIGGER reject_accept'); conn.commit()
    assert w.service.action(item['id'], 'accept', w.sid, w.epoch)['accepted_message_id']
    assert w.ds.get_message_count(w.sid) == 2


def test_http_accept_uses_its_own_lease_without_calling_a_model(presence_world, monkeypatch):
    w = presence_world; enable(w); item = publish(w)
    from core import datastore
    monkeypatch.setattr(datastore, '_datastore_instance', w.ds)
    monkeypatch.setattr(w.service, '_busy', PresenceService._busy.__get__(w.service))
    app = create_app(autostart=False); install_state_gate(app)
    client = app.test_client()
    response = client.post(f"/api/presence/inbox/{item['id']}/accept", json={'session_id': w.sid, 'avatar_id': '', 'dataset_epoch': w.epoch})
    assert response.status_code == 200 and response.json['item']['accepted_message_id']
    assert len(w.calls) == 1 and w.ds.get_message_count(w.sid) == 2


def test_native_transport_is_bounded_without_proxies_redirects_retries_or_tools(monkeypatch):
    import openai
    from core.presence import _call
    calls = []
    class FakeOpenAI:
        def __init__(self, **kwargs):
            calls.append(kwargs)
            self.chat = NS(completions=NS(create=self.create))
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def create(self, **kwargs):
            calls.append(kwargs)
            return NS(choices=[NS(message=NS(content='A synthetic hello', tool_calls=None))])
    monkeypatch.setattr(openai, 'OpenAI', FakeOpenAI)
    assert _call([{'role': 'user', 'content': 'Synthetic'}], {'base_url': 'http://127.0.0.1/v1', 'model': 'fake'}) == 'A synthetic hello'
    assert calls[0]['max_retries'] == 0 and not calls[0]['http_client'].follow_redirects
    assert calls[0]['http_client'].trust_env is False and calls[0]['timeout'].read == 15
    assert calls[1]['max_tokens'] == 256 and 'tools' not in calls[1]


def test_full_backup_restore_pauses_presence_invalidates_inbox_and_clears_expression(backup_world, monkeypatch):
    from core.expression import get_expression, record_exchange
    b = backup_world
    user = b.store.save_message(b.sid, 'user', 'A synthetic milestone')
    assistant = b.store.save_message(b.sid, 'assistant', "I'm glad to hear that.")
    epoch = b.store.dataset_epoch()['value']
    assert record_exchange(b.store, b.sid, user, assistant, NOW)
    provider = {'provider_key': 'local', 'model': 'synthetic', 'base_url': 'http://127.0.0.1:1234/v1'}
    service = PresenceService(b.store, clock=lambda: NOW, provider_getter=lambda: provider,
        context_getter=lambda *a: {'personality': config.get_personality(), 'avatar': None, 'notes': {}, 'memories': [],
                                  'sources': [reference(b.store.get_message(user))], 'conversation': 'Synthetic'},
        llm=lambda *a, **kw: 'A synthetic check-in')
    b.store._presence_service = service
    monkeypatch.setattr(service, '_busy', lambda: False)
    w = NS(ds=b.store, sid=b.sid, epoch=epoch, service=service)
    enable(w); item = publish(w)
    accepted = service.action(item['id'], 'accept', b.sid, epoch)['accepted_message_id']
    assert get_expression(b.store, '', NOW)['state'] == 'happy'
    snapshot = b.service.create()
    preview = b.service.preview(b.service.archive_path(snapshot['id']))
    b.service.apply(preview['token'])
    pref = service.preferences(b.sid)['preferences']
    assert not pref['enabled'] and pref['needs_review']
    inbox = service.inbox(b.sid)
    assert inbox['items'][0]['state'] == 'stale' and inbox['items'][0]['text'] == ''
    assert inbox['jobs'][0]['state'] == 'stale'
    assert get_expression(b.store, '', NOW)['state'] == 'calm'
    assert b.store.get_message(accepted) and accepted not in [m.id for m in prompt_messages(b.store, b.sid, b.store.get_messages(b.sid))]


def test_production_context_is_owned_bounded_and_has_no_chat_photo_or_model_side_effects(knowledge_world, monkeypatch):
    w = knowledge_world
    w.ds.save_message(w.a_sid, 'user', 'A_SOURCE_ONLY')
    w.ds.save_message(w.b_sid, 'user', 'OTHER_CONVERSATION_SECRET')
    w.service.remember(w.a, 'A_MEMORY_ONLY')
    w.service.remember(w.b, 'OTHER_MEMORY_SECRET')
    w.service.remember('', 'SHARED_MEMORY_SECRET')
    w.notes.store.write(w.a, about_you='A_NOTE_ONLY', our_story='Derived story')
    w.notes.store.write(w.b, about_you='OTHER_NOTE_SECRET')
    def forbidden(*a, **kw): raise AssertionError('Presence must use pure context, without chat/photo/model side effects')
    monkeypatch.setattr(w.engine, 'build_context', forbidden)
    service = PresenceService(w.ds, clock=lambda: NOW,
        provider_getter=lambda: {'provider_key': 'local', 'model': 'fake', 'base_url': 'http://127.0.0.1:1/v1'})
    data = service._context(w.a_sid, w.a, 0)
    prompt = json.dumps(service._prompt(data, 'greeting'))
    assert all(token in prompt for token in ('A_SOURCE_ONLY', 'A_MEMORY_ONLY', 'A_NOTE_ONLY'))
    assert not any(token in prompt for token in ('OTHER_CONVERSATION_SECRET', 'OTHER_MEMORY_SECRET', 'SHARED_MEMORY_SECRET', 'OTHER_NOTE_SECRET'))
    assert len(data['memories']) <= 8 and len(data['conversation']) <= 3000 and not w.calls
    assert service._context(w.a_sid, w.a, 2 ** 62)['conversation'] == ''


def test_notebook_consolidation_never_treats_accepted_presence_as_new_evidence(knowledge_world, monkeypatch):
    w = knowledge_world
    user = w.ds.save_message(w.a_sid, 'user', 'USER_FACT_ONLY')
    w.ds.save_message(w.a_sid, 'assistant', 'ORDINARY_REPLY_ONLY')
    service = PresenceService(w.ds, clock=lambda: NOW,
        provider_getter=lambda: {'provider_key': 'local', 'model': 'fake', 'base_url': 'http://127.0.0.1:1/v1'},
        llm=lambda *a, **kw: 'DERIVED_INBOX_MARKER')
    w.ds._presence_service = service
    monkeypatch.setattr(service, '_busy', lambda: False)
    p = NS(ds=w.ds, sid=w.a_sid, epoch=w.ds.dataset_epoch()['value'], service=service)
    enable(p); item = publish(p)
    accepted = service.action(item['id'], 'accept', p.sid, p.epoch)['accepted_message_id']
    assert accepted in [m.id for m in prompt_messages(w.ds, p.sid, w.ds.get_messages(p.sid))]
    assert w.notes.run_now(w.a, p.sid)['ok']
    sent = json.dumps(w.calls[-1])
    assert 'USER_FACT_ONLY' in sent and 'ORDINARY_REPLY_ONLY' not in sent and 'DERIVED_INBOX_MARKER' not in sent
    w.ds.change_message(user, p.sid, content='CORRECTED_USER_FACT')
    w.service.repair(w.a)
    assert w.notes.run_now(w.a, p.sid)['ok']
    assert 'DERIVED_INBOX_MARKER' not in json.dumps(w.calls[-1])


# ---------------------------------------------------------------- browser notifications (checkin-notices.js)

def test_notices_list_new_unread_check_ins_without_their_text_unless_asked(presence_world):
    w = presence_world; enable(w)
    start = w.service.notices()
    assert start['notices'] == [] and start['now'] == NOW.timestamp()          # no "after": watching starts now
    item = publish(w)
    listed = w.service.notices(start['now'] - 1)['notices']
    assert [n['id'] for n in listed] == [item['id']] and 'text' not in listed[0]
    assert listed[0]['session_id'] == w.sid and listed[0]['kind'] and listed[0]['name']
    assert w.service.notices(start['now'] - 1, include_text=True)['notices'][0]['text'] == item['text']
    assert w.service.notices(listed[0]['created_at'])['notices'] == []         # seen once, not again
    assert w.calls and len(w.calls) == 1                                       # reading notices never asks the model


@pytest.mark.parametrize('action', ['read', 'dismiss', 'accept'])
def test_a_check_in_that_was_read_dismissed_or_added_is_no_longer_announced(presence_world, action):
    w = presence_world; enable(w); item = publish(w)
    w.service.action(item['id'], action, w.sid, w.epoch)
    assert w.service.notices(0)['notices'] == []


def test_an_expired_or_invalidated_check_in_is_not_announced(presence_world):
    w = presence_world; enable(w); publish(w)
    w.clock[0] += timedelta(days=2)
    assert w.service.notices(0)['notices'] == []
    w.clock[0] = NOW + timedelta(minutes=16)
    publish(w, 'request_two')
    with w.ds._connect() as conn:
        conn.execute('UPDATE presence_inbox SET invalidated=1')
        conn.commit()
    assert w.service.notices(0)['notices'] == []


def test_a_kids_pals_check_in_is_never_announced(world):
    from core import kids, pals
    kids.enable(True)
    made = pals.create(world.avatars, 'Pip', 'sunny', '', engine='kokoro', kids=True)
    other = pals.create(world.avatars, 'Nova', 'geeky', '', engine='kokoro')
    service = PresenceService(world.store, clock=lambda: NOW)
    epoch = world.store.dataset_epoch()['value']
    sessions = {owner: world.store.ensure_avatar_session(owner, owner) for owner in (made['id'], other['id'])}
    with service.connection() as conn:
        for owner, sid in sessions.items():
            conn.execute('INSERT INTO presence_inbox(id,job_id,avatar_id,session_id,dataset_epoch,kind,text,created_at,expires_at,snapshot) '
                         'VALUES(?,?,?,?,?,?,?,?,?,?)', ('pi_' + owner, 'pj_' + owner, owner, sid, epoch, 'checkin', 'Hello',
                                                         NOW.timestamp(), NOW.timestamp() + 3600, '{}'))
        conn.commit()
    assert [n['name'] for n in service.notices(0)['notices']] == ['Nova']


def test_the_notices_route_reads_only_and_refuses_odd_arguments(presence_world, monkeypatch):
    w = presence_world
    from core import datastore
    from core.routes.lock import KIDS_ALLOWED
    monkeypatch.setattr(datastore, '_datastore_instance', w.ds)
    app = create_app(autostart=False); install_state_gate(app)
    client = app.test_client()
    response = client.get('/api/presence/notices?after=0')
    assert response.status_code == 200 and response.headers['Cache-Control'] == 'no-store'
    assert set(response.get_json()) == {'now', 'notices'}
    for query in ('after=soon', 'after=-1', 'after=nan', 'session_id=1', 'after=1&after=2'):
        assert client.get('/api/presence/notices?' + query).status_code == 400, query
    assert 'presence.notices' not in KIDS_ALLOWED                                  # the kids view is refused it
    assert w.calls == []
