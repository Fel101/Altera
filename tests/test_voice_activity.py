"""Short local speech checks use fake VAD only, never Whisper or real inference."""
import importlib
import struct
import threading
from types import SimpleNamespace as NS

import numpy as np
import pytest

from core import voice_activity as activity
from core.app import create_app
from core.datastore import get_datastore
from core.state_gate import STATE


@pytest.fixture
def vad(tmp_path, monkeypatch):
    calls, loads = [], []
    asset = tmp_path / 'fake-vad.onnx'
    asset.write_bytes(b'not a model; inference is always fake')

    def model(samples):
        calls.append(samples.copy())
        return np.full(len(samples) // 512, .8, dtype=np.float32)

    def load(path):
        loads.append(path)
        return model

    monkeypatch.setattr(activity, '_installed_asset', lambda: asset)
    monkeypatch.setattr(activity, '_load_model', load)
    monkeypatch.setattr(activity, '_model', None)
    monkeypatch.setattr(activity, '_load_failed', False)
    monkeypatch.setattr(activity, '_work_lock', threading.Lock())
    return NS(calls=calls, loads=loads, asset=asset, model=model)


@pytest.fixture
def activity_app(altera_config, vad):
    app = create_app(autostart=False)
    store = get_datastore()
    sid = store.create_session('Synthetic voice activity')
    store.set_key('dataset_epoch', {'value': 'activity-epoch', 'required': True})
    return NS(app=app, client=app.test_client(use_cookies=False), store=store, sid=sid,
              headers={'X-Audio-Sample-Rate': '16000', 'X-Altera-Dataset': 'activity-epoch'}, vad=vad)


def post_frame(world, body=b'\0\0' * 5120, *, session_id=None, headers=None, content_type='audio/pcm'):
    return world.client.post(f'/api/voice/activity?session_id={world.sid if session_id is None else session_id}',
                             data=body, content_type=content_type, headers=world.headers if headers is None else headers)


def test_pcm_is_normalized_padded_and_model_is_cached(vad):
    pcm = struct.pack('<hh', -32768, 32767) + b'\0\0' * 511
    first, second = activity.detect_pcm16(pcm), activity.detect_pcm16(pcm)
    assert first['speech'] is True and first['samples'] == 513 and first['sample_rate'] == 16000
    assert first == second and len(vad.loads) == 1 and len(vad.calls) == 2
    assert vad.calls[0].shape == (1024,) and vad.calls[0].dtype == np.float32
    assert vad.calls[0][0] == -1 and vad.calls[0][1] == pytest.approx(32767 / 32768)
    assert not vad.calls[0][513:].any()


@pytest.mark.parametrize(('probabilities', 'speech'), [([.1] * 10, False), ([.8, .1] * 5, False),
                                                    ([.1, .8, .8] + [.1] * 7, True)])
def test_speech_requires_consecutive_model_windows(vad, monkeypatch, probabilities, speech):
    monkeypatch.setattr(activity, '_model', lambda samples: probabilities)
    result = activity.detect_pcm16(b'\0\0' * 5120)
    assert result['speech'] is speech and result['probability'] == max(probabilities)


def test_full_second_is_bounded_to_32_windows(vad):
    result = activity.detect_pcm16(b'\0\0' * 16000)
    assert result['samples'] == 16000 and vad.calls[0].shape == (16384,)


@pytest.mark.parametrize('audio', [b'', b'\0' * 1023, b'\0' * 1025, b'\0' * 32002, 'not bytes'],
                         ids=['empty', 'short', 'odd', 'long', 'wrong-type'])
def test_invalid_pcm_never_loads_or_calls_model(vad, audio):
    with pytest.raises(activity.VoiceActivityError) as error:
        activity.detect_pcm16(audio)
    assert error.value.status == 400 and vad.loads == [] and vad.calls == []


def test_busy_detector_rejects_without_queuing_and_releases_after_work(vad, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    results = []

    def load(asset):
        vad.loads.append(asset)
        entered.set()
        assert release.wait(3)
        return vad.model

    monkeypatch.setattr(activity, '_load_model', load)
    worker = threading.Thread(target=lambda: results.append(activity.detect_pcm16(b'\0\0' * 5120)))
    worker.start()
    try:
        assert entered.wait(2)
        with pytest.raises(activity.VoiceActivityError) as error:
            activity.detect_pcm16(b'\0\0' * 5120)
        assert error.value.code == 'voice_activity_busy' and error.value.status == 409
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive() and results[0]['available'] is True and len(vad.loads) == 1
    assert activity.detect_pcm16(b'\0\0' * 5120)['available'] is True and len(vad.loads) == 1


@pytest.mark.parametrize('output', [[float('nan')] * 10, [float('inf')] * 10, [1.1] * 10, [-.1] * 10, [.8]])
def test_invalid_native_results_disable_polling_safely(vad, monkeypatch, output):
    monkeypatch.setattr(activity, '_model', lambda samples: output)
    with pytest.raises(activity.VoiceActivityError) as error:
        activity.detect_pcm16(b'\0\0' * 5120)
    assert error.value.status == 503 and activity.availability()['available'] is False
    with pytest.raises(activity.VoiceActivityError):
        activity.detect_pcm16(b'\0\0' * 5120)
    assert vad.loads == []


def test_model_load_failure_is_private_and_not_retried_each_frame(vad, monkeypatch):
    def broken(asset):
        vad.loads.append(asset)
        raise OSError('private-installed-path and native failure details')
    monkeypatch.setattr(activity, '_load_model', broken)
    for _ in range(2):
        with pytest.raises(activity.VoiceActivityError) as error:
            activity.detect_pcm16(b'\0\0' * 5120)
        assert 'private-installed-path' not in str(error.value)
    assert len(vad.loads) == 1


def test_missing_asset_never_loads_a_model(vad, monkeypatch):
    monkeypatch.setattr(activity, '_installed_asset', lambda: None)
    assert activity.availability()['available'] is False
    with pytest.raises(activity.VoiceActivityError) as error:
        activity.detect_pcm16(b'\0\0' * 5120)
    assert error.value.status == 503 and vad.loads == []


def test_installed_probe_does_not_import_or_load_models(tmp_path, monkeypatch):
    package = tmp_path / 'faster_whisper'
    (package / 'assets').mkdir(parents=True)
    (package / 'assets/silero_vad_v6.onnx').write_bytes(b'fake installed asset')
    def find(name):
        return NS(submodule_search_locations=[str(package)]) if name == 'faster_whisper' else NS()
    monkeypatch.setattr(importlib.util, 'find_spec', find)
    monkeypatch.setattr(activity, '_model', None)
    monkeypatch.setattr(activity, '_load_failed', False)
    monkeypatch.setattr(activity, '_load_model', lambda path: pytest.fail('An availability probe must not load a model'))
    assert activity.availability() == {'available': True, 'loaded': False, 'engine': 'silero-vad-v6', 'processing': 'local-cpu',
                                       'sample_rate': 16000, 'min_samples': 512, 'max_samples': 16000}


def test_route_returns_exact_session_and_does_not_save_audio(activity_app, monkeypatch, altera_config):
    from core import companion
    world = activity_app
    monkeypatch.setattr(companion, 'get_services', lambda: pytest.fail('An unowned session must not use the active avatar'))
    before = set(altera_config.parent.rglob('*'))
    response = post_frame(world)
    assert response.status_code == 200
    assert response.json['session_id'] == world.sid and response.json['avatar_id'] is None
    assert response.json['available'] is True and response.json['speech'] is True
    assert response.json['samples'] == 5120 and response.json['sample_rate'] == 16000
    assert world.store.get_message_count(world.sid) == 0 and set(altera_config.parent.rglob('*')) == before
    assert response.headers['Cache-Control'] == 'no-store'


def test_route_verifies_owned_avatar_without_falling_back_to_active(activity_app, monkeypatch):
    from core import companion
    world, looked_up = activity_app, []
    with world.store._connect() as conn:
        conn.execute('UPDATE sessions SET avatar_id=? WHERE id=?', ('owned-avatar', world.sid))
    monkeypatch.setattr(companion, 'get_services', lambda: NS(avatars=NS(get=lambda owner: looked_up.append(owner) or {'id': owner})))
    response = post_frame(world)
    assert response.status_code == 200 and response.json['avatar_id'] == 'owned-avatar'
    assert looked_up == ['owned-avatar']
    monkeypatch.setattr(companion, 'get_services', lambda: NS(avatars=NS(get=lambda owner: None)))
    response = post_frame(world)
    assert response.status_code == 404 and len(world.vad.calls) == 1


@pytest.mark.parametrize('session_id', ['', '0', '-1', '1.5', 'true', '100000000000000000000', '9223372036854775808', '١'])
def test_route_rejects_invalid_session(activity_app, session_id):
    response = post_frame(activity_app, session_id=session_id)
    assert response.status_code == 400 and activity_app.vad.calls == []


def test_route_rejects_missing_session_before_loading_model(activity_app):
    response = post_frame(activity_app, session_id=999999)
    assert response.status_code == 404 and activity_app.vad.loads == []


@pytest.mark.parametrize('body', [b'', b'\0' * 1023, b'\0' * 1025], ids=['empty', 'short', 'odd'])
def test_route_rejects_invalid_frame(activity_app, body):
    response = post_frame(activity_app, body=body)
    assert response.status_code == 400 and activity_app.vad.calls == []


def test_oversized_body_and_unsupported_json_are_rejected_before_global_json_parsing(activity_app, monkeypatch):
    from flask import Request
    def no_json(*args, **kwargs):
        pytest.fail('Unsupported VAD payload must not enter the global JSON parser')
    monkeypatch.setattr(Request, 'get_json', no_json)
    assert post_frame(activity_app, body=b'\0' * 32002).status_code == 413
    assert post_frame(activity_app, body=b'{"samples":[]}', content_type='application/json').status_code == 400
    assert activity_app.vad.loads == []


@pytest.mark.parametrize('rate', [None, '8000', '16000.0', '44100'])
def test_route_rejects_unsupported_rate(activity_app, rate):
    headers = {**activity_app.headers}
    if rate is None:
        headers.pop('X-Audio-Sample-Rate')
    else:
        headers['X-Audio-Sample-Rate'] = rate
    assert post_frame(activity_app, headers=headers).status_code == 400
    assert activity_app.vad.loads == []


@pytest.mark.parametrize('headers', [{'Origin': 'http://evil.example'}, {'Sec-Fetch-Site': 'cross-site'}])
def test_route_has_global_cross_site_protection(activity_app, headers):
    response = post_frame(activity_app, headers={**activity_app.headers, **headers})
    assert response.status_code == 403 and response.json['code'] == 'cross_site_request'
    assert activity_app.vad.loads == []


@pytest.mark.parametrize('epoch', [None, 'older-dataset'])
def test_route_rejects_missing_or_stale_dataset(activity_app, epoch):
    headers = {**activity_app.headers}
    if epoch is None:
        headers.pop('X-Altera-Dataset')
    else:
        headers['X-Altera-Dataset'] = epoch
    response = post_frame(activity_app, headers=headers)
    assert response.status_code == 409 and response.json['code'] == 'dataset_changed'
    assert activity_app.vad.calls == []


def test_route_has_maintenance_gate_and_bounded_busy_response(activity_app):
    with STATE.exclusive():
        response = post_frame(activity_app)
        assert response.status_code == 503 and response.json['code'] == 'state_busy'
    with activity._work_lock:
        response = post_frame(activity_app)
        assert response.status_code == 409 and response.json['code'] == 'voice_activity_busy'
    assert activity_app.vad.loads == []
