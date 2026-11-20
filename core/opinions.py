"""
Altera — Opinion Formation & Humor Style Matching (Phase 10)

Tracks mild opinions on discussed topics and matches user's humor style.
Develops personality-consistent perspectives that can evolve over time.
"""

import logging
import json
from datetime import datetime
from core.constants import LLM_TEMPERATURE_ANALYSIS, LLM_MAX_TOKENS_LONG, LLM_MAX_TOKENS_DEFAULT
from core.datastore import get_datastore

LOGGER = logging.getLogger(__name__)


class OpinionTracker:
    """Tracks mild opinions the AI forms on frequently discussed topics."""

    def __init__(self):
        self.ds = get_datastore()

    def analyze_opinions(self, text, session_id=None):
        """Analyze message for potential opinion formation on a topic."""
        prompt = f"""Analyze this message to identify if the user expresses an opinion or preference on any topic:

Message: "{text}"

Return ONLY a JSON object with no markdown or extra text:
{{"topics_with_opinions": [{{"topic": "...", "opinion": "...", "strength": 0.5}}]}}

Rules:
- Topics can be hobbies, preferences, views on subjects, etc.
- Opinion should capture the user's stance or preference
- Strength should be between 0.1 (mild) and 1.0 (strong)"""

        try:
            from core.llm import ask

            content = ask("You are an opinion extraction engine. Return ONLY valid JSON.", prompt,
                          temperature=LLM_TEMPERATURE_ANALYSIS, max_tokens=LLM_MAX_TOKENS_LONG)

            # Clean markdown wrapping
            if content.startswith("```"):
                lines = [line for line in content.split("\n") if not line.strip().startswith("```")]
                content = "\n".join(lines)

            result = json.loads(content)

            topics_with_opinions = result.get("topics_with_opinions", [])

            if session_id and topics_with_opinions:
                self._store_opinions(topics_with_opinions, session_id)

            return topics_with_opinions

        except Exception:
            LOGGER.exception("Analysis failed")
            return []

    def _store_opinions(self, opinions_data, session_id):
        """Store extracted opinions in the database."""
        # Load existing opinions via DataStore or initialize
        opinions_db = self.ds.get_user_opinions()

        for item in opinions_data:
            topic = item.get("topic", "").lower().strip()
            if not topic:
                continue

            opinion = item.get("opinion", "")
            strength = min(max(item.get("strength", 0.5), 0.1), 1.0)

            if topic not in opinions_db:
                opinions_db[topic] = {
                    "opinion": opinion,
                    "first_seen": datetime.now().isoformat(),
                    "last_updated": datetime.now().isoformat(),
                    "mention_count": 1,
                    "strength": strength,
                    "contexts": [session_id or "unknown"]
                }
            else:
                # Update existing opinion if new one is stronger or more recent
                opinions_db[topic]["last_updated"] = datetime.now().isoformat()
                opinions_db[topic]["mention_count"] += 1

                # If the new opinion contradicts the old one, increase strength to reflect discussion
                if (opinions_db[topic]["opinion"].lower() not in opinion.lower() and
                    opinion.lower() not in opinions_db[topic]["opinion"].lower()):
                    opinions_db[topic]["strength"] = min(opinions_db[topic]["strength"] + 0.1, 1.0)
                else:
                    # Reinforce existing opinion
                    opinions_db[topic]["strength"] = min(opinions_db[topic]["strength"] + (strength * 0.1), 1.0)

                if session_id and session_id not in opinions_db[topic]["contexts"]:
                    opinions_db[topic]["contexts"].append(session_id)

        # Save back via DataStore
        self.ds.set_user_opinions(opinions_db)

    def get_user_opinions(self):
        """Get all stored user opinions."""
        return self.ds.get_user_opinions()


class HumorMatcher:
    """Matches the user's humor style (sarcastic, dry, playful, etc.)."""

    def __init__(self):
        self.ds = get_datastore()

    def analyze_humor_style(self, text_samples=None, session_id=None):
        """Analyze the user's humor style from recent messages."""
        if not text_samples:
            # Get recent user messages across all sessions via DataStore
            ds = get_datastore()
            sessions = ds.list_sessions(archived=False)
            all_messages = []
            for session in sessions[:10]:  # Check up to 10 most recent sessions
                msgs = ds.get_user_messages(session.id, limit=5)
                all_messages.extend(msgs)
            text_samples = all_messages[-10:] if all_messages else []

        if not text_samples:
            return {"style": "playful", "level": "medium"}

        combined_text = "\n".join(text_samples)

        prompt = f"""Analyze the humor style in these messages and return ONLY a JSON object with no markdown or extra text:

{combined_text}

Return exactly this JSON structure (no markdown, no code blocks):
{{"style": "sarcastic|dry|playful|witty|observational|self-deprecating",
"level": "low|medium|high",
"is_joking": boolean}}"""

        try:
            from core.llm import ask

            content = ask("You are a humor style analysis engine. Return ONLY valid JSON.", prompt,
                          temperature=LLM_TEMPERATURE_ANALYSIS, max_tokens=LLM_MAX_TOKENS_DEFAULT)

            # Clean markdown wrapping
            if content.startswith("```"):
                lines = [line for line in content.split("\n") if not line.strip().startswith("```")]
                content = "\n".join(lines)

            result = json.loads(content)

            # Validate structure
            valid_styles = {"sarcastic", "dry", "playful", "witty", "observational", "self-deprecating"}
            if result.get("style") not in valid_styles:
                result["style"] = "playful"

            valid_levels = {"low", "medium", "high"}
            if result.get("level") not in valid_levels:
                result["level"] = "medium"

            result.setdefault("is_joking", False)

            if session_id:
                self._store_humor_style(result, session_id)

            return result

        except Exception:
            LOGGER.exception("Analysis failed")
            return {"style": "playful", "level": "medium", "is_joking": False}

    def _store_humor_style(self, style_data, session_id):
        """Store the humor style in the database."""
        # Load existing humor style via DataStore or initialize
        humor_data = self.ds.get_humor_style()

        if not humor_data:
            humor_data = {"style": "playful", "level": "medium", "is_joking": False}

        # Update with new data
        humor_data.update(style_data)
        humor_data["last_analyzed"] = datetime.now().isoformat()

        self.ds.set_humor_style(humor_data)

    def get_humor_style(self):
        """Get the stored humor style."""
        return self.ds.get_humor_style()


# Singleton instances
opinion_tracker = OpinionTracker()
humor_matcher = HumorMatcher()
