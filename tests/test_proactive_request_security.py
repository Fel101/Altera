"""A cross-site browser read must not trigger or store a proactive model reply."""
from types import SimpleNamespace as NS

import pytest

from core.app import create_app
from core.datastore import get_datastore


@pytest.fixture
def proactive_app(altera_config, monkeypatch):
    from core import config, proactive
    altera_config.write_text('voice:\n  tts_enabled: false\nimagegen:\n  enabled: false\n'
                          'phase4:\n  proactive_enabled: true\n  proactive_interval_hours: 6\n', encoding='utf-8')
    config.LOCAL_CONFIG_PATH.write_text('{}\n', encoding='utf-8')
    config.reset_cache()
    app = create_app(autostart=False)
    store = get_datastore()
    sid = store.create_session('Synthetic quiet chat')
    store.save_message(sid, 'user', 'Synthetic private context')
    with store._connect() as conn:
        conn.execute("UPDATE messages SET timestamp=datetime('now','-8 hours') WHERE session_id=?", (sid,))
    asked = []
    client = NS(chat=lambda messages: asked.append(messages) or 'Synthetic check-in')
    monkeypatch.setattr(proactive, 'get_client', lambda: client)
    return NS(client=app.test_client(use_cookies=False), store=store, sid=sid, asked=asked)


@pytest.mark.parametrize('method', ['GET', 'HEAD'])
@pytest.mark.parametrize('headers', [{'Sec-Fetch-Site': 'cross-site'}, {'Origin': 'http://evil.example'},
                                     {'Origin': 'https://localhost'}, {'Origin': 'null'}, {'Origin': 'http://['}])
def test_cross_site_proactive_read_does_not_call_model_or_persist_state(proactive_app, method, headers):
    w = proactive_app
    response = w.client.open(f'/api/proactive/checkin?session_id={w.sid}', method=method, headers=headers)
    assert response.status_code == 403
    assert w.asked == [] and w.store.get_key('last_proactive_message') is None


@pytest.mark.parametrize('method', ['GET', 'HEAD'])
def test_same_origin_legacy_proactive_read_never_runs_inference(proactive_app, method):
    w = proactive_app
    response = w.client.open(f'/api/proactive/checkin?session_id={w.sid}', method=method,
                             headers={'Sec-Fetch-Site': 'same-origin', 'Origin': 'http://localhost'})
    assert response.status_code == 200 and w.asked == []
    assert w.store.get_key('last_proactive_message') is None
    if method == 'GET':
        assert response.json['message'] is None and response.json['requires_opt_in']


def test_other_read_only_get_routes_keep_their_existing_origin_behavior(proactive_app):
    w = proactive_app
    response = w.client.get('/api/offline/status', headers={'Sec-Fetch-Site': 'cross-site'})
    assert response.status_code == 200 and response.json['online'] is True
    assert w.asked == []
