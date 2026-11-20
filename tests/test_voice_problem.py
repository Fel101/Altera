"""A voice sample that stays silent says why: not installed, not running, still loading, or that one voice is missing.

It used to say "Speech is switched off in Settings" for every failure, even when speech was on and only the chosen voice
(Michael, say) was not in the voice service.
"""
import pytest

from core import tts, voice_service
from test_blueprints import create_app


def kokoro(voice="am_michael", **extra):
    return tts.settings({"voice": {"tts_engine": "kokoro", "kokoro": {"voice": voice, **extra}}})


@pytest.fixture
def service(monkeypatch):
    state = {"info": None, "installed": True}
    monkeypatch.setattr(voice_service, "info", lambda cfg=None, timeout=0.7: state["info"])
    monkeypatch.setattr(voice_service, "installed", lambda cfg=None: state["installed"])
    return state


def test_a_voice_the_service_does_not_have_is_named_with_the_command_that_adds_it(service):
    service["info"] = {"loaded": ["kokoro"], "voices": ["af_bella", "af_heart"]}
    said = voice_service.problem(kokoro())
    assert said.startswith("Michael is not installed in the voice service.")
    assert "serve.py --download --voices am_michael" in said and "switched off" not in said


def test_a_voice_service_of_your_own_is_not_given_altera_s_command(service):
    service["info"] = {"loaded": ["kokoro"], "voices": ["af_heart"]}
    said = voice_service.problem(kokoro(python="C:/lab/python.exe", script="C:/lab/serve.py"))
    assert said == "Michael is not installed in your voice service. Add the voice am_michael to it, or choose another."


@pytest.mark.parametrize("info, installed, start", [
    (None, False, "The local voice is not installed."),
    (None, True, "The local voice service is not running yet."),
    ({"loaded": []}, True, "The local voice is still loading."),
    ({"loaded": ["kokoro"], "voices": ["am_michael"]}, True, "The local voice could not speak as Michael."),
    ({"loaded": ["kokoro"]}, True, "The local voice could not speak as Michael."),      # a service that lists no voices
])
def test_each_other_reason_is_said_plainly(service, info, installed, start):
    service.update(info=info, installed=installed)
    assert voice_service.problem(kokoro()).startswith(start)


def test_the_online_voice_points_at_the_internet(service):
    assert "internet" in voice_service.problem(tts.settings({"voice": {"tts_engine": "edge"}}))


def test_the_sample_route_returns_the_reason_for_the_voice_it_was_asked_for(service, monkeypatch):
    import core.voice_layer
    monkeypatch.setattr(core.voice_layer, "get_tts_audio", lambda text, person_voice=None: None)
    chosen = kokoro("af_heart")
    monkeypatch.setattr(tts, "settings", lambda config=None: chosen)
    service["info"] = {"loaded": ["kokoro"], "voices": ["af_heart"]}
    answer = create_app("engagement").test_client().post("/api/voice/tts", json={"text": "Hi.", "voice": "am_michael"})
    assert answer.status_code == 200
    assert answer.get_json()["audio_url"] is None
    assert answer.get_json()["message"].startswith("Michael is not installed")
