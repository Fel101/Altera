"""
Altera — Emotion Detection & Emotional Memory (Phase 4)

Uses LM Studio to analyze emotional tone of messages, stores emotional context
alongside factual memories, and provides mood-aware response guidance.
"""

import logging
import json
import re
from datetime import datetime, timedelta
import threading
from core.constants import LLM_TEMPERATURE_ANALYSIS, LLM_MAX_TOKENS_DEFAULT
from core.datastore import get_datastore

LOGGER = logging.getLogger(__name__)


# Module-level lock for LLM calls to prevent concurrent request race conditions
_llm_lock = threading.Lock()


# ============================================================
# Emotion detection via LM Studio
# ============================================================

def detect_emotion(text, llm_client=None):
    """
    Analyze the emotional tone of a message using the LLM.

    Returns dict with:
        - primary_emotion: one of 'happy', 'sad', 'angry', 'anxious',
                           'excited', 'calm', 'lonely', 'grateful',
                           'frustrated', 'neutral'
        - intensity: 0.0-1.0 (how strong the emotion is)
        - valence: -1.0 to 1.0 (negative to positive)
        - secondary_emotions: list of related emotions
        - analysis: brief explanation
    """
    prompt = f"""Analyze the emotional tone of this message and return ONLY a JSON object with no markdown or extra text:

Message: "{text}"

Return exactly this JSON structure (no markdown, no code blocks):
{{"primary_emotion": "...", "intensity": 0.5, "valence": 0.0, "secondary_emotions": [], "analysis": "..."}}

Emotion categories for primary_emotion: happy, sad, angry, anxious, excited, calm, lonely, grateful, frustrated, neutral
Intensity should be between 0.0 (barely noticeable) and 1.0 (very strong).
Valence should be between -1.0 (very negative) and 1.0 (very positive)."""

    if llm_client is None:
        try:
            from core.llm import LLMClient, get_provider_registry_instance
            registry = get_provider_registry_instance()
            active_provider = registry.get_active_provider()
            model_name = registry.get_active_model() or (active_provider.default_model if active_provider else "")

            llm_client = LLMClient(
                base_url=active_provider.base_url if active_provider else 'http://localhost:1234/v1',
                api_key=active_provider.api_key if active_provider else 'lm-studio',
                model=model_name,
                temperature=LLM_TEMPERATURE_ANALYSIS,
                max_tokens=_analysis_tokens()
            )
        except Exception:
            LOGGER.exception("Cannot connect to LLM for emotion detection")
            return _default_emotion(text)

    try:
        # Use the module-level LLM lock to prevent concurrent requests racing for the same model slot
        with _llm_lock:
            response_text = llm_client.chat(
                messages=[
                    {"role": "system", "content": "You are an emotion analysis engine. Return ONLY valid JSON, no markdown formatting."},
                    {"role": "user", "content": prompt}
                ],
                stream=False
            )

        result = _json_answer(response_text)

        # Validate and fill defaults
        valid_emotions = {"happy", "sad", "angry", "anxious", "excited", "calm",
                         "lonely", "grateful", "frustrated", "neutral"}
        if result.get("primary_emotion") not in valid_emotions:
            result["primary_emotion"] = "neutral"

        result.setdefault("intensity", 0.5)
        result.setdefault("valence", 0.0)
        result.setdefault("secondary_emotions", [])
        result.setdefault("analysis", "")

        # Clamp values
        result["intensity"] = max(0.0, min(1.0, float(result["intensity"])))
        result["valence"] = max(-1.0, min(1.0, float(result["valence"])))

        return result

    except (json.JSONDecodeError, Exception):
        LOGGER.exception("LLM analysis failed, falling back to keyword detection")
        return _default_emotion(text)


_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)
_OBJECT = re.compile(r"\{.*\}", re.S)


def _json_answer(text) -> dict:
    """The JSON object in the model's answer: reasoning (<think>...</think>), code fences and words around it are left out.
    Raises ValueError when there is none."""
    found = _OBJECT.search(_THINK.sub("", text or ""))
    if not found:
        raise ValueError("The model's answer has no JSON object.")
    result = json.loads(found.group(0))
    if not isinstance(result, dict):
        raise ValueError("The model's answer is not a JSON object.")
    return result


def _analysis_tokens() -> int:
    """The answer's length, and room for a reasoning model to think before it (it otherwise spends all of it thinking)."""
    from core.llm import ANSWER_ROOM
    return LLM_MAX_TOKENS_DEFAULT + ANSWER_ROOM


def _default_emotion(text):
    """Fallback keyword-based emotion detection."""
    text_lower = text.lower()

    positive_words = ["happy", "great", "love", "excited", "good", "amazing",
                      "wonderful", "fantastic", "awesome", "beautiful", "thank",
                      "grateful", "blessed", "joy", "pleased", "delighted",
                      "proud", "satisfied", "hopeful", "optimistic"]
    negative_words = ["sad", "depressed", "angry", "frustrated", "stressed",
                      "worried", "anxious", "tired", "exhausted", "hate",
                      "terrible", "awful", "horrible", "pain", "suffer",
                      "overwhelming", "hopeless", "lonely", "empty", "lost",
                      "disappointed", "annoyed", "irritated", "upset", "down"]
    lonely_words = ["alone", "lonely", "nobody", "no one", "isolated",
                    "empty", "miss you", "missing", "abandoned"]

    score = 0
    for w in positive_words:
        if w in text_lower:
            score += 1
    for w in negative_words:
        if w in text_lower:
            score -= 1
    for w in lonely_words:
        if w in text_lower:
            score -= 2

    if score >= 3:
        return {"primary_emotion": "happy", "intensity": min(0.5 + score * 0.1, 1.0),
                "valence": min(score / 4.0, 1.0), "secondary_emotions": [], "analysis": ""}
    elif score >= 2:
        return {"primary_emotion": "excited", "intensity": min(0.6 + score * 0.08, 1.0),
                "valence": min(score / 3.5, 1.0), "secondary_emotions": [], "analysis": ""}
    elif score > 0:
        return {"primary_emotion": "calm", "intensity": min(0.3 + score * 0.15, 1.0),
                "valence": max(0.0, score / 4.0), "secondary_emotions": [], "analysis": ""}
    elif score <= -2:
        return {"primary_emotion": "sad", "intensity": min(0.5 + abs(score) * 0.1, 1.0),
                "valence": max(-1.0, score / 4.0), "secondary_emotions": [], "analysis": ""}
    elif score <= -1:
        return {"primary_emotion": "frustrated", "intensity": min(0.3 + abs(score) * 0.15, 1.0),
                "valence": max(-1.0, score / 4.0), "secondary_emotions": [], "analysis": ""}
    else:
        return {"primary_emotion": "neutral", "intensity": 0.3, "valence": 0.0,
                "secondary_emotions": [], "analysis": ""}


# ============================================================
# Emotional memory storage
# ============================================================

class EmotionalMemoryStore:
    """Stores emotional context alongside factual memories."""

    def __init__(self):
        pass

    def add_emotional_context(self, session_id, message_text, emotion_data, role="user"):
        """Add emotional metadata to a message in the database."""

        store = get_datastore()
        store.update_message_mood(session_id, role, {
            "emotion": emotion_data["primary_emotion"],
            "intensity": emotion_data.get("intensity", 0.5),
            "valence": emotion_data.get("valence", 0.0),
            "secondary_emotions": emotion_data.get("secondary_emotions", []),
            "analysis": emotion_data.get("analysis", ""),
            "timestamp": datetime.now().isoformat()
        })

    def get_emotional_history(self, session_id=None, limit=30):
        """Get recent emotional history — reads from user_profile.yaml."""
        from core.profile import load_user_profile
        profile = load_user_profile()

        history = profile.get("mood_history", [])
        results = []

        for entry in reversed(history[-limit:]):  # newest first, limited
            results.append({
                "emotion": entry.get("emotion", "neutral"),
                "intensity": entry.get("intensity", 0.3),
                "valence": entry.get("valence", 0.0),
                "timestamp": entry.get("timestamp", ""),
                "message_preview": entry.get("message_preview", ""),
                "analysis": "",
                "secondary_emotions": [],
                "message_id": None
            })

        return results

    def get_mood_trend(self, days=7):
        """Get mood trend over the last N days — reads from user_profile.yaml."""
        from core.profile import load_user_profile
        profile = load_user_profile()

        history = profile.get("mood_history", [])
        cutoff = datetime.now() - timedelta(days=days)

        moods = []
        for entry in reversed(history):  # oldest first
            try:
                ts = datetime.fromisoformat(entry.get("timestamp", ""))
                if ts >= cutoff:
                    moods.append(entry.get("emotion", "neutral"))
            except (ValueError, TypeError):
                pass

        # Calculate trend
        if not moods:
            return {"trend": "unknown", "dominant_mood": "neutral", "count": 0}

        from collections import Counter
        counts = Counter(moods)
        dominant = counts.most_common(1)[0][0]

        # Determine trend direction (compare first half vs second half)
        mid = len(moods) // 2
        first_half = moods[:mid] if mid > 0 else moods
        second_half = moods[mid:] if mid > 0 else []

        positive_first = sum(1 for m in first_half if m in {"happy", "excited", "grateful"})
        negative_first = sum(1 for m in first_half if m in {"sad", "angry", "anxious", "frustrated"})
        positive_second = sum(1 for m in second_half if m in {"happy", "excited", "grateful"})
        negative_second = sum(1 for m in second_half if m in {"sad", "angry", "anxious", "frustrated"})

        first_ratio = (positive_first - negative_first) / max(len(first_half), 1)
        second_ratio = (positive_second - negative_second) / max(len(second_half), 1)

        if second_ratio > first_ratio + 0.2:
            trend = "improving"
        elif second_ratio < first_ratio - 0.2:
            trend = "declining"
        else:
            trend = "stable"

        return {
            "trend": trend,
            "dominant_mood": dominant,
            "count": len(moods),
            "breakdown": dict(counts)
        }


# ============================================================
# Mood-aware response guidance
# ============================================================

def get_emotion_guidance(emotion_data):
    """Generate guidance for how the AI should respond based on detected emotion."""
    if not emotion_data or emotion_data.get("primary_emotion") == "neutral":
        return ""

    guidance = {
        "happy": "\n\nThe user seems happy right now. Match their positive energy — be warm and celebratory, but don't overdo it.",
        "sad": "\n\nThe user seems sad or down. Be gentle and supportive. Ask what's on their mind. Offer comfort without being overly dramatic.",
        "angry": "\n\nThe user seems frustrated or angry. Stay calm and validating. Don't argue — acknowledge their feelings first before anything else.",
        "anxious": "\n\nThe user seems anxious or worried. Be reassuring and grounding. Help them feel heard and understood. Offer gentle perspective if appropriate.",
        "excited": "\n\nThe user is excited! Match that energy — show genuine enthusiasm and ask follow-up questions about what's got them so energized.",
        "calm": "\n\nThe user seems relaxed and at peace. This is a good time for deeper conversation or light, pleasant topics.",
        "lonely": "\n\nThe user seems lonely. Be extra warm and present. Let them know you're here for them. Ask about how they've been feeling lately.",
        "grateful": "\n\nThe user is expressing gratitude. Accept it gracefully and reflect it back — maybe share something positive in return.",
        "frustrated": "\n\nThe user seems frustrated. Be patient and helpful. Acknowledge the difficulty, then offer to help or just listen if that's what they need.",
    }

    emotion = emotion_data.get("primary_emotion", "neutral")
    intensity = emotion_data.get("intensity", 0.5)

    # Scale guidance by intensity
    base_guidance = guidance.get(emotion, "")

    if intensity > 0.7:
        return f"{base_guidance} (Strong signal — pay extra attention to this.)"
    elif intensity < 0.3:
        return f"{base_guidance} (Subtle mood — keep it natural, don't make it obvious you're tracking it.)"

    return base_guidance


# ============================================================
# Singleton instance
# ============================================================

emotional_store = EmotionalMemoryStore()


def analyze_and_store_emotion(text, session_id=None, role="user"):
    """Analyze emotion and store it — convenience function used by altera.py."""
    # Try to get an LLM client for detection
    llm_client = None
    try:
        from core.llm import LLMClient, get_provider_registry_instance
        registry = get_provider_registry_instance()
        active_provider = registry.get_active_provider()
        model_name = registry.get_active_model() or (active_provider.default_model if active_provider else "")

        llm_client = LLMClient(
            base_url=active_provider.base_url if active_provider else 'http://localhost:1234/v1',
            api_key=active_provider.api_key if active_provider else 'lm-studio',
            model=model_name,
            temperature=LLM_TEMPERATURE_ANALYSIS,
            max_tokens=_analysis_tokens()
        )
    except Exception:
        LOGGER.exception("Cannot connect to LLM for emotion detection")

    # Detect emotion (use LLM if available, fall back to keyword)
    try:
        emotion_data = detect_emotion(text, llm_client=llm_client)
    except Exception:
        LOGGER.exception("Detection failed, using default")
        emotion_data = _default_emotion(text)

    if session_id:
        emotional_store.add_emotional_context(session_id, text, emotion_data, role)

    return emotion_data


# Tests: analyse in the calling thread, so what was analysed can be looked at as soon as the reply is done.
RUN_INLINE = False


def analyze_later(text, session_id=None, role="user") -> None:
    """analyze_and_store_emotion in the background. The reply is finished (and the next message can be sent) without
    waiting for it: a reasoning model can think about the mood for half a minute."""
    def job():
        try:
            analyze_and_store_emotion(text, session_id=session_id, role=role)
        except Exception:
            LOGGER.exception("Emotion analysis error after the reply")
    if RUN_INLINE:
        job()
        return
    from core.state_gate import start_job
    start_job(job, name="altera-emotion")
