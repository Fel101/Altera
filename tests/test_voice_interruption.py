"""Interrupt audio without losing saved text or waiting for a native synthesizer."""
import json
import threading
import time

import pytest

from core import notes, prompts, voice_layer
from core.chat import ChatContext
from core.state_gate import STATE, StateBusy
from core.turns import get_turn_service
from test_companion_api import engine, tmp_store, world  # noqa: F401
from test_polish_reliability import reliable  # noqa: F401


def test_cancelled_speech_drains_without_waiting_but_retains_running_writer_lease():
    started, release, cancelled, drained = (threading.Event() for _ in range(4))
    calls, result = [], []

    def synth(text):
        calls.append(text)
        started.set()
        assert release.wait(5)
        return '/static/audio/tts/late.wav'

    speech = voice_layer.SpeechStream(synth=synth, cancel_event=cancelled)
    try:
        speech.feed('This first sentence is long enough to begin speaking. ')
        speech.feed('The next sentence is still being written')
        assert started.wait(2)
        speech.feed('A second sentence waits for the first synthesizer to finish. ')
        speech.finish()
        cancelled.set()

        def drain():
            result.extend(speech.drain(timeout=5))
            drained.set()

        worker = threading.Thread(target=drain)
        worker.start()
        assert drained.wait(1), 'Stop must not wait for the in-progress native synthesis'
        worker.join(1)
        assert result == [] and len(calls) == 1
        with pytest.raises(StateBusy):
            with STATE.exclusive():
                pytest.fail('The native writer must remain protected until it actually exits')
    finally:
        release.set()
        speech.close()
        speech._pool.shutdown(wait=True, cancel_futures=True)
    assert len(calls) == 1


def test_cancel_before_first_text_creates_no_speech_jobs():
    cancelled = threading.Event()
    cancelled.set()
    calls = []
    speech = voice_layer.SpeechStream(synth=lambda text: calls.append(text), cancel_event=cancelled)
    try:
        speech.feed('This sentence should never be synthesized after cancellation.')
        speech.finish()
        assert speech.drain() == [] and calls == []
    finally:
        speech.close()


def test_stop_during_saved_reply_speech_preserves_answer_and_finishes_stream(reliable, engine, world, altera_config, monkeypatch):
    from core import profile
    started, release, done = (threading.Event() for _ in range(3))
    altera_config.write_text('voice:\n  tts_enabled: true\n  cloud_fallback: false\n', 'utf-8')
    monkeypatch.setattr(profile, 'load_user_profile', lambda: {'preferences': {'voice_enabled': True}})

    def synth(*args, **kwargs):
        started.set()
        assert release.wait(5)
        return '/static/audio/tts/late.wav'

    monkeypatch.setattr(voice_layer, 'get_tts_audio', synth)
    text = 'Here is a complete reply that has already been saved before its speech finishes.'
    engine.llm.chunks = [text]
    response = reliable.post('/api/chat/stream', json={'message': 'Hello', 'request_id': 'saved-speech-interrupt'}, buffered=False)
    service, events, collector = get_turn_service(), [], None
    try:
        assert started.wait(2)
        deadline = time.monotonic() + 3
        while not service.get('saved-speech-interrupt')['assistant_message_id'] and time.monotonic() < deadline:
            time.sleep(.01)
        row = service.get('saved-speech-interrupt')
        assert row['assistant_message_id']
        stopped = reliable.post('/api/chat/requests/saved-speech-interrupt/stop')
        assert stopped.status_code == 200 and stopped.json['status'] == 'saved'

        def collect():
            events.extend(service.events('saved-speech-interrupt'))
            done.set()

        collector = threading.Thread(target=collect)
        collector.start()
        assert done.wait(1), 'Saved text should settle while the cancelled synthesizer is still blocked'
        assert events[-1]['status'] == 'saved'
        assert not any(e.get('audio_url') for e in events)
        assert world.store.get_message(row['assistant_message_id']).content == text
        assert service.get('saved-speech-interrupt')['error'] in (None, '')
    finally:
        release.set()
        response.close()
        if collector:
            collector.join(3)
        # The turn is done before speech, so explicitly wait for the writer lease.
        deadline = time.monotonic() + 3
        while any(v == 'altera-speech' for v in STATE.active.values()) and time.monotonic() < deadline:
            time.sleep(.01)


@pytest.mark.parametrize('avatar', [None, {'id': 'mara-test', 'card': {'name': 'Mara', 'age': 30, 'personality': []}}])
def test_all_personas_receive_personal_evidence_rules(avatar):
    from core.config import get_personality
    builder = prompts.PromptBuilder(get_personality(), {})
    ctx = ChatContext(avatar=avatar, notes={'about_you': '- Maybe enjoys hiking', 'our_story': '- An imagined trip'})
    prompt = builder.build(ctx)
    assert 'never invent a shared past' in prompt
    assert 'current explicit user correction takes priority' in prompt
    assert 'not evidence of real events' in prompt
    assert 'derived summaries' in prompt and 'incomplete provenance' in prompt
    assert 'trust them' not in prompt


def test_note_rewrite_cannot_promote_assistant_guesses_or_imagined_scenes():
    request = notes.build_request('Mara', {'about_you': '- Maybe likes tea'}, 'Them: Let us imagine a trip.\nMara: You live in Paris.', 450)
    text = json.dumps(request)
    assert 'not independent evidence' in text
    assert 'Never promote assistant guesses' in text
    assert 'explicit current corrections' in text
    assert 'imagined shared scene' in text
