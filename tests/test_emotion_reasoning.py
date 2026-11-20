"""Mood analysis with a reasoning model: room to think, its JSON read past the thinking, and run after the reply is done."""
import threading

import pytest

from core import emotion
from core.constants import LLM_MAX_TOKENS_DEFAULT
from core.llm import ANSWER_ROOM


class Model:
    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def chat(self, messages, stream=False):
        self.calls.append(messages)
        return self.answer


@pytest.mark.parametrize("answer", [
    '{"primary_emotion": "anxious", "intensity": 0.6, "valence": -0.2}',
    '<think>They want reassurance. {"not": "this"}?</think>\n{"primary_emotion": "anxious", "intensity": 0.6, "valence": -0.2}',
    '```json\n{"primary_emotion": "anxious", "intensity": 0.6, "valence": -0.2}\n```',
    'Here you go: {"primary_emotion": "anxious", "intensity": 0.6, "valence": -0.2} Hope that helps.',
])
def test_the_json_is_read_past_thinking_fences_and_chatter(answer):
    found = emotion.detect_emotion("do you love me?", llm_client=Model(answer))
    assert found["primary_emotion"] == "anxious" and found["intensity"] == 0.6 and found["valence"] == -0.2


@pytest.mark.parametrize("answer", ["", "<think>still thinking when the tokens ran out", "[1, 2]"])
def test_no_usable_answer_falls_back_to_the_keywords(answer):
    text = "I am so happy today"
    assert emotion.detect_emotion(text, llm_client=Model(answer)) == emotion._default_emotion(text)


def test_the_model_gets_room_to_think_before_it_answers(monkeypatch):
    made = []

    class Client:
        def __init__(self, **kwargs):
            made.append(kwargs)

        def chat(self, messages, stream=False):
            return '{"primary_emotion": "calm"}'
    import core.llm
    monkeypatch.setattr(core.llm, "LLMClient", Client)
    monkeypatch.setattr(emotion.emotional_store, "add_emotional_context", lambda *a, **k: None)
    assert emotion.analyze_and_store_emotion("fine", session_id=1)["primary_emotion"] == "calm"
    assert made[0]["max_tokens"] == LLM_MAX_TOKENS_DEFAULT + ANSWER_ROOM


def test_after_a_reply_the_mood_is_analysed_in_the_background(monkeypatch):
    monkeypatch.setattr(emotion, "RUN_INLINE", False)
    started, release, seen = threading.Event(), threading.Event(), []

    def slow(text, session_id=None, role="user"):
        started.set()
        release.wait(5)
        seen.append((text, session_id, role))
    monkeypatch.setattr(emotion, "analyze_and_store_emotion", slow)
    emotion.analyze_later("hello", session_id=7)               # returns while the analysis is still running
    assert started.wait(5) and seen == []
    release.set()
    for _ in range(100):
        if seen:
            break
        threading.Event().wait(0.05)
    assert seen == [("hello", 7, "user")]


def test_a_failing_analysis_in_the_background_is_only_logged(monkeypatch, caplog):
    def broken(*a, **k):
        raise RuntimeError("synthetic")
    monkeypatch.setattr(emotion, "analyze_and_store_emotion", broken)
    emotion.analyze_later("hello", session_id=7)                # inline in tests: nothing is raised
    assert "Emotion analysis error after the reply" in caplog.text
