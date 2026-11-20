"""Ordinary transcription never downloads weights implicitly."""
import sys
from types import SimpleNamespace

import pytest

from core import stt


def test_missing_tokenizer_stops_before_whisper_can_use_online_fallback(tmp_path, monkeypatch):
    loaded = []
    monkeypatch.setitem(sys.modules, 'faster_whisper', SimpleNamespace(WhisperModel=lambda *a, **kw: loaded.append(a)))
    stt.reset_models()
    try:
        result = stt.transcribe_audio('synthetic.wav', model_size=str(tmp_path))
        assert result == {'text': '', 'error': stt.TRANSCRIPTION_FAILED}
        assert loaded == []
    finally:
        stt.reset_models()


def test_named_model_resolution_is_offline_and_requires_its_tokenizer(tmp_path, monkeypatch):
    from faster_whisper import utils
    calls = []
    def resolve(name, **options):
        calls.append((name, options))
        return str(tmp_path)
    monkeypatch.setattr(utils, 'download_model', resolve)
    assert not (tmp_path / 'tokenizer.json').exists()
    try:
        stt._local_model_path('tiny')
    except FileNotFoundError:
        pass
    else:
        raise AssertionError('An incomplete cached model must be refused.')
    (tmp_path / 'tokenizer.json').write_text('{}', encoding='utf-8')
    assert stt._local_model_path('tiny') == str(tmp_path)
    assert calls == [('tiny', {'local_files_only': True}), ('tiny', {'local_files_only': True})]


def test_model_status_checks_cached_assets_without_loading_or_network(tmp_path, monkeypatch):
    from faster_whisper import utils
    calls = []
    def resolve(name, **options):
        calls.append((name, options))
        return str(tmp_path)
    monkeypatch.setattr(utils, 'download_model', resolve)
    monkeypatch.setattr('faster_whisper.WhisperModel', lambda *a, **kw: pytest.fail('Status must not load a model.'))
    stt.reset_models()
    assert stt.model_status('tiny') == 'unavailable'
    for name in ('tokenizer.json', 'model.bin', 'config.json'):
        (tmp_path / name).write_bytes(b'synthetic')
    assert stt.model_status('tiny') == 'cached'
    assert calls == [('tiny', {'local_files_only': True}), ('tiny', {'local_files_only': True})]


def test_model_status_does_not_leak_failure_and_recognizes_loaded_model(monkeypatch):
    stt.reset_models()
    monkeypatch.setattr(stt, '_local_model_path', lambda name: (_ for _ in ()).throw(OSError('private path')))
    assert stt.model_status('missing') == 'unavailable'
    stt._models['loaded'] = object()
    try:
        assert stt.model_status('loaded') == 'loaded'
    finally:
        stt.reset_models()


@pytest.mark.parametrize('installed,state', [(True, 'unavailable'), (True, 'cached'), (False, 'not_installed')])
def test_voice_status_reports_local_model_without_starting_speech(monkeypatch, altera_config, installed, state):
    from core import voice_service
    from test_blueprints import create_app
    import importlib.util
    altera_config.write_text('voice:\n  stt_model: synthetic-model\n  tts_enabled: false\n', encoding='utf-8')
    original = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, 'find_spec', lambda name, *a, **kw:
                        object() if name == 'faster_whisper' and installed else
                        None if name == 'faster_whisper' else original(name, *a, **kw))
    monkeypatch.setattr(stt, 'model_status', lambda name: state if installed and name == 'synthetic-model'
                        else pytest.fail('Unexpected model check.'))
    monkeypatch.setattr(voice_service, 'ensure_started_async', lambda *a: pytest.fail('Status started speech.'))
    result = create_app('engagement').test_client().get('/api/voice/status')
    assert result.status_code == 200
    assert result.json['stt_available'] is installed and result.json['stt_model_state'] == state


def test_default_transcription_uses_cached_cpu_weights_only(monkeypatch):
    built = []

    class Model:
        def __init__(self, size, **options):
            built.append((size, options))

        def transcribe(self, path, **options):
            return [SimpleNamespace(text='Local words')], SimpleNamespace(language='en', language_probability=.9)

    monkeypatch.setitem(sys.modules, 'faster_whisper', SimpleNamespace(WhisperModel=Model))
    monkeypatch.setattr(stt, '_local_model_path', lambda size: size)
    stt.reset_models()
    try:
        assert stt.transcribe_audio('local-recording.wav', model_size='offline-unit')['text'] == 'Local words'
        assert built == [('offline-unit', {'device': 'cpu', 'compute_type': 'int8', 'local_files_only': True})]
    finally:
        stt.reset_models()


def test_missing_cached_weights_returns_actionable_error_without_retry_or_download(monkeypatch):
    calls = []

    def missing(size, **options):
        calls.append(options)
        assert options['local_files_only'] is True
        raise FileNotFoundError('private cache path')

    monkeypatch.setitem(sys.modules, 'faster_whisper', SimpleNamespace(WhisperModel=missing))
    monkeypatch.setattr(stt, '_local_model_path', lambda size: size)
    stt.reset_models()
    try:
        result = stt.transcribe_audio('local-recording.wav', model_size='missing-offline-unit')
        assert result['text'] == '' and 'local speech model is installed' in result['error']
        assert 'private cache path' not in result['error'] and len(calls) == 1
    finally:
        stt.reset_models()
