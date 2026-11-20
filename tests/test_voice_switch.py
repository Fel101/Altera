"""Switching Voice on in Settings turns the local voice on in config.yaml, so nobody has to edit the file by hand.

Only the local voice, only the first time, and never the online voice, which sends every reply to Microsoft.
"""
import pytest
import yaml

from core import voice_service
from core.config import reset_cache
from test_blueprints import create_app


@pytest.fixture
def started(monkeypatch):
    calls = []
    monkeypatch.setattr(voice_service, 'installed', lambda cfg=None: True)
    monkeypatch.setattr(voice_service, 'ensure_started_async', lambda cfg=None: calls.append(cfg) or True)
    return calls


def switch(enabled):
    reset_cache()
    response = create_app('engagement').test_client().post('/api/voice/toggle', json={'enabled': enabled})
    assert response.status_code == 200
    reset_cache()


def voice_of(path):
    return (yaml.safe_load(path.read_text(encoding='utf-8')) or {}).get('voice')


STARTER = {'tts_enabled': False, 'tts_engine': 'kokoro', 'cloud_fallback': False,
           'kokoro': {'url': 'http://127.0.0.1:8770', 'autostart': False}}


def test_switching_on_turns_the_local_voice_on_and_starts_it(altera_config, started):
    altera_config.write_text(yaml.safe_dump({'voice': STARTER}), encoding='utf-8')
    switch(True)
    voice = voice_of(altera_config)
    assert voice['tts_enabled'] is True and voice['kokoro']['autostart'] is True
    assert voice['tts_engine'] == 'kokoro' and voice['kokoro']['url'] == 'http://127.0.0.1:8770'
    assert len(started) == 1


def test_a_voice_that_is_not_installed_is_switched_on_but_not_started(altera_config, started, monkeypatch):
    monkeypatch.setattr(voice_service, 'installed', lambda cfg=None: False)
    altera_config.write_text(yaml.safe_dump({'voice': STARTER}), encoding='utf-8')
    switch(True)
    assert voice_of(altera_config)['tts_enabled'] is True
    assert started == []


def test_switching_off_changes_only_the_preference(altera_config, started):
    altera_config.write_text(yaml.safe_dump({'voice': STARTER}), encoding='utf-8')
    switch(False)
    assert voice_of(altera_config) == STARTER
    assert started == []


@pytest.mark.parametrize('voice', [
    {'tts_enabled': False, 'tts_engine': 'edge'},                         # Microsoft's online voice
    {'tts_enabled': False},                                               # no engine: the code falls back to edge
    {'tts_enabled': False, 'tts_engine': 'kokoro', 'kokoro': {'url': 'http://192.168.1.20:8770'}},
    {'tts_enabled': False, 'tts_engine': 'kokoro', 'kokoro': {'python': 'C:/other/python.exe', 'script': 'x.py'}},
    {'tts_enabled': True, 'tts_engine': 'kokoro', 'kokoro': {'autostart': False}},   # already on: left alone
])
def test_anything_but_altera_s_own_local_voice_switched_on_for_the_first_time_is_left_alone(altera_config, started,
                                                                                         voice):
    altera_config.write_text(yaml.safe_dump({'voice': voice}), encoding='utf-8')
    before = altera_config.read_text(encoding='utf-8')
    switch(True)
    assert altera_config.read_text(encoding='utf-8') == before
    assert started == []


def test_a_config_with_no_voice_section_at_all_is_left_alone(altera_config, started):
    altera_config.write_text('voice:\n', encoding='utf-8')
    switch(True)
    assert voice_of(altera_config) is None
    assert started == []
