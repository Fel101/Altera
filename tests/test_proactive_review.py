"""Check-in prompts respect the same recall boundaries as ordinary chat."""
from types import SimpleNamespace

import pytest

from core.proactive import ProactiveManager
from test_companion_api import tmp_store  # noqa: F401


@pytest.fixture
def checkins(tmp_store, monkeypatch):
    calls = []
    monkeypatch.setattr('core.proactive.get_client', lambda: SimpleNamespace(
        chat=lambda messages: calls.append(messages) or 'Checking in'))
    return ProactiveManager(tmp_store), calls


def chat(store, owner):
    sid = store.create_session('Test companion')
    store.set_session_avatar(sid, owner)
    return sid


def boundary(store, owner, through):
    store.set_key('knowledge_recall:' + (owner or '@default'), {'through': through})


@pytest.mark.parametrize('owner', ['', 'mara-bell-47ab97'])
def test_forgotten_history_never_reaches_a_checkin_model(tmp_store, checkins, owner):
    manager, calls = checkins
    sid = chat(tmp_store, owner)
    forgotten = tmp_store.save_message(sid, 'user', 'Private sailing plan to forget')
    boundary(tmp_store, owner, forgotten)
    assert manager.get_proactive_message(sid) is None
    assert calls == []
    assert tmp_store.get_message(forgotten).content == 'Private sailing plan to forget'


def test_checkin_uses_only_new_eligible_chat_without_shared_derived_context(tmp_store, checkins, monkeypatch):
    manager, calls = checkins
    sid = chat(tmp_store, 'mara-bell-47ab97')
    forgotten = tmp_store.save_message(sid, 'user', 'Forgotten private sailing plan')
    boundary(tmp_store, 'mara-bell-47ab97', forgotten)
    tmp_store.save_message(sid, 'user', 'I planted a new rose garden')
    monkeypatch.setattr(tmp_store, 'get_user_profile', lambda: {'emotion_history': [{'category': 'private-old-mood'}]})
    manager.followup_tracker.get_pending_followups = lambda limit=5: [
        {'priority': 'high', 'item': 'Forgotten appointment', 'context': 'private sailing'}]
    assert manager.get_proactive_message(sid) == 'Checking in'
    prompt = calls[0][1]['content']
    assert 'rose garden' in prompt
    assert 'sailing' not in prompt and 'Forgotten' not in prompt
    assert 'Recent mood trend' not in prompt


def test_implicit_session_resolves_last_active_and_uses_its_boundary(tmp_store, checkins):
    manager, calls = checkins
    sid = chat(tmp_store, 'mara-bell-47ab97')
    forgotten = tmp_store.save_message(sid, 'user', 'Forgotten travel plan')
    boundary(tmp_store, 'mara-bell-47ab97', forgotten)
    other = chat(tmp_store, 'ana-cruz-11aa22')
    tmp_store.save_message(other, 'user', 'Another companion private chat')
    tmp_store.set_last_active_session(sid)
    assert manager.get_proactive_message() is None
    assert calls == []
    tmp_store.save_message(sid, 'user', 'New knitting project')
    assert manager.get_proactive_message() == 'Checking in'
    assert 'knitting' in calls[0][1]['content']
    assert 'travel' not in calls[0][1]['content'] and 'Another companion' not in calls[0][1]['content']


def test_legacy_across_session_fallback_filters_each_owner_and_shared_context(tmp_store, checkins, monkeypatch):
    manager, calls = checkins
    first = chat(tmp_store, 'mara-bell-47ab97')
    forgotten = tmp_store.save_message(first, 'user', 'Forgotten sailing plan')
    boundary(tmp_store, 'mara-bell-47ab97', forgotten)
    second = chat(tmp_store, 'ana-cruz-11aa22')
    tmp_store.save_message(second, 'user', 'Current gardening project')
    monkeypatch.setattr(tmp_store, 'get_user_profile', lambda: {'emotion_history': [{'category': 'private-old-mood'}]})
    manager.followup_tracker.get_pending_followups = lambda limit=5: [
        {'priority': 'high', 'item': 'Forgotten appointment', 'context': 'sailing'}]
    assert manager.get_proactive_message() == 'Checking in'
    prompt = calls[0][1]['content']
    assert 'gardening' in prompt
    assert 'sailing' not in prompt and 'Forgotten appointment' not in prompt
    assert 'Recent mood trend' not in prompt


def test_no_eligible_across_session_history_skips_the_model(tmp_store, checkins):
    manager, calls = checkins
    sid = chat(tmp_store, '')
    forgotten = tmp_store.save_message(sid, 'user', 'Forgotten default companion history')
    boundary(tmp_store, '', forgotten)
    assert manager.get_proactive_message() is None
    assert calls == []


def test_checkin_manager_does_not_store_a_reply_when_all_context_was_forgotten(tmp_store, checkins, monkeypatch):
    manager, calls = checkins
    sid = chat(tmp_store, 'mara-bell-47ab97')
    forgotten = tmp_store.save_message(sid, 'user', 'Forgotten old chat')
    boundary(tmp_store, 'mara-bell-47ab97', forgotten)
    monkeypatch.setattr('core.proactive.proactive_settings', lambda: {'enabled': True, 'interval_hours': 6})
    monkeypatch.setattr(manager, 'should_proactively_engage', lambda *args: True)
    result = manager.check_and_generate(sid)
    assert result['message'] is None and result['type'] == 'none'
    assert calls == [] and tmp_store.get_key('last_proactive_message') is None
