"""Cross-feature journey using a disposable database and a synthetic provider."""
from types import SimpleNamespace

from core.app import create_app
from core.episodes import EpisodeService
from core.knowledge import KnowledgeService
from core.presence import PresenceService
from core.state_gate import install_state_gate
from test_presence import presence_world  # noqa: F401


def test_ritual_story_report_and_forget_share_source_boundaries(presence_world, monkeypatch):
    from core import datastore
    from core.routes import episodes, knowledge, presence

    w = presence_world
    monkeypatch.setattr(datastore, '_datastore_instance', w.ds)
    monkeypatch.setattr(w.service, '_busy', PresenceService._busy.__get__(w.service))
    voice_invalidations = []
    w.ds._voice_note_service = SimpleNamespace(invalidate_owner=voice_invalidations.append)
    manager = SimpleNamespace(store=SimpleNamespace(get_all=lambda **_kwargs: []))
    knowledge_service = KnowledgeService(w.ds, manager)
    episode_service = EpisodeService(w.ds, knowledge_factory=lambda _store: knowledge_service)
    monkeypatch.setattr(presence, 'get_presence_service', lambda: w.service)
    monkeypatch.setattr(episodes, 'get_episode_service', lambda: episode_service)
    monkeypatch.setattr(knowledge, 'get_knowledge_service', lambda: knowledge_service)
    app = create_app(autostart=False)
    install_state_gate(app)
    client = app.test_client()
    scope = {'session_id': w.sid, 'dataset_epoch': w.epoch, 'avatar_id': ''}

    assert client.get(f'/api/presence/inbox?session_id={w.sid}').json['items'] == []
    assert client.post('/api/presence/check-now', json={**scope, 'request_id': 'journey_disabled'}).status_code == 409
    assert w.calls == []

    response = client.patch('/api/presence/preferences', json={**scope, 'revision': 0,
        'settings': {'enabled': True, 'quiet_enabled': False, 'cooldown_minutes': 15, 'daily_cap': 2}})
    assert response.status_code == 200
    response = client.post('/api/presence/rituals', json={**scope, 'kind': 'greeting', 'minute': 15 * 60, 'enabled': True})
    assert response.status_code == 201
    assert w.calls == []
    w.service.tick()
    assert w.service.run_one() and len(w.calls) == 1
    item = client.get(f'/api/presence/inbox?session_id={w.sid}').json['items'][0]
    assert item['kind'] == 'greeting' and item['state'] == 'available'
    response = client.post(f"/api/presence/inbox/{item['id']}/accept", json=scope)
    assert response.status_code == 200
    accepted = response.json['item']['accepted_message_id']

    report_url = '/api/knowledge/scopes/_default/report'
    report = client.get(report_url)
    assert report.status_code == 200 and report.headers['Cache-Control'] == 'no-store'
    candidates = report.json['conversations'][0]['history_candidates']
    assert {w.user, accepted} <= {message['message_id'] for message in candidates}

    response = client.post('/api/episodes', json={**scope, 'request_id': 'journey_episode',
        'kind': 'actual_exchange', 'title': 'A tea conversation', 'in_story': True,
        'sources': [{'message_id': w.user}]})
    assert response.status_code == 201
    episode = response.json['episode']
    response = client.patch('/api/episodes/' + episode['id'], json={**scope, 'revision': episode['revision'],
        'title': 'A corrected tea conversation', 'pinned': True})
    assert response.status_code == 200
    episode = response.json['episode']
    assert episode['in_story'] and episode['pinned'] and episode['title'] == 'A corrected tea conversation'
    assert client.get(f'/api/episodes/recall?session_id={w.sid}&query=jasmine').json['episodes']

    w.ds.change_message(w.user, w.sid, content='I prefer green tea now.')
    episode_after_edit = client.get(f'/api/episodes?session_id={w.sid}').json['episodes'][0]
    assert episode_after_edit['status'] == 'stale' and episode_after_edit['title'] == ''
    assert client.get(f'/api/episodes/recall?session_id={w.sid}&query=jasmine').json['episodes'] == []
    assert client.get(f'/api/presence/inbox?session_id={w.sid}').json['items'][0]['state'] == 'stale'
    candidates = client.get(report_url).json['conversations'][0]['history_candidates']
    assert accepted not in {message['message_id'] for message in candidates}

    response = client.delete('/api/episodes/' + episode['id'], json={**scope, 'revision': episode['revision'], 'confirm': 'forget'})
    assert response.status_code == 200 and response.json['episode']['status'] == 'forgotten'
    assert voice_invalidations == ['']
    assert w.ds.get_message(w.user) and w.ds.get_message(accepted).content == item['text']
    assert client.get(f'/api/episodes?session_id={w.sid}').json['episodes'] == []
    assert client.get(f'/api/episodes/recall?session_id={w.sid}&query=jasmine').json['episodes'] == []
    assert client.get(f'/api/presence/inbox?session_id={w.sid}').json['items'][0]['state'] == 'stale'
    report = client.get(report_url).json
    assert report['recall']['after'] >= accepted
    assert all(not conversation['history_candidates'] for conversation in report['conversations'])
    assert len(w.calls) == 1
