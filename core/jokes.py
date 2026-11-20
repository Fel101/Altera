"""
Altera — Inside Joke System (Phase 10)

Tracks recurring topics, inside jokes, and shared references from past conversations.
Provides natural callbacks to build a deeper connection with the user.
"""

import logging
import json
from datetime import datetime
from core.constants import LLM_TEMPERATURE_ANALYSIS, LLM_MAX_TOKENS_LONG
from core.datastore import get_datastore

LOGGER = logging.getLogger(__name__)


class JokeTracker:
    """Tracks inside jokes and shared references from conversations."""

    def __init__(self):
        self.ds = get_datastore()

    def extract_jokes_and_references(self, text, session_id=None):
        """Extract potential jokes, recurring topics, or shared references from a message."""
        prompt = f"""Analyze this message and identify any inside jokes, recurring topics, or shared references:

Message: "{text}"

Return ONLY a JSON object with no markdown or extra text:
{{"jokes": ["potential joke 1"], "recurring_topics": ["topic 1", "topic 2"], "shared_references": ["reference 1"]}}

Rules:
- Jokes are humorous statements, punchlines, or funny observations
- Recurring topics are subjects the user has mentioned multiple times or with enthusiasm
- Shared references are specific phrases, quotes, or unique concepts between you and the user"""

        try:
            from core.llm import ask

            content = ask("You are an inside joke and reference extraction engine. Return ONLY valid JSON.", prompt,
                          temperature=LLM_TEMPERATURE_ANALYSIS, max_tokens=LLM_MAX_TOKENS_LONG)

            # Clean markdown wrapping
            if content.startswith("```"):
                lines = [line for line in content.split("\n") if not line.strip().startswith("```")]
                content = "\n".join(lines)

            result = json.loads(content)

            # Validate structure
            result.setdefault("jokes", [])
            result.setdefault("recurring_topics", [])
            result.setdefault("shared_references", [])

            if session_id:
                self._store_jokes_and_references(result, session_id)

            return result

        except Exception:
            LOGGER.exception("Extraction failed")
            return {"jokes": [], "recurring_topics": [], "shared_references": []}

    def _store_jokes_and_references(self, data, session_id):
        """Store extracted jokes and references in the database."""
        # Load existing jokes data via DataStore or initialize
        jokes_data = self.ds.get_jokes_and_references()

        if not jokes_data:
            jokes_data = {"jokes": {}, "recurring_topics": {}, "shared_references": {}}

        # Process jokes
        for joke in data.get("jokes", []):
            joke_key = joke.lower().strip()
            if joke_key not in jokes_data["jokes"]:
                jokes_data["jokes"][joke_key] = {
                    "text": joke,
                    "first_seen": datetime.now().isoformat(),
                    "mention_count": 1,
                    "contexts": [session_id or "unknown"]
                }
            else:
                jokes_data["jokes"][joke_key]["mention_count"] += 1
                if session_id and session_id not in jokes_data["jokes"][joke_key]["contexts"]:
                    jokes_data["jokes"][joke_key]["contexts"].append(session_id)

        # Process recurring topics
        for topic in data.get("recurring_topics", []):
            topic_key = topic.lower().strip()
            if topic_key not in jokes_data["recurring_topics"]:
                jokes_data["recurring_topics"][topic_key] = {
                    "text": topic,
                    "first_seen": datetime.now().isoformat(),
                    "mention_count": 1,
                    "contexts": [session_id or "unknown"]
                }
            else:
                jokes_data["recurring_topics"][topic_key]["mention_count"] += 1
                if session_id and session_id not in jokes_data["recurring_topics"][topic_key]["contexts"]:
                    jokes_data["recurring_topics"][topic_key]["contexts"].append(session_id)

        # Process shared references
        for ref in data.get("shared_references", []):
            ref_key = ref.lower().strip()
            if ref_key not in jokes_data["shared_references"]:
                jokes_data["shared_references"][ref_key] = {
                    "text": ref,
                    "first_seen": datetime.now().isoformat(),
                    "mention_count": 1,
                    "contexts": [session_id or "unknown"]
                }
            else:
                jokes_data["shared_references"][ref_key]["mention_count"] += 1
                if session_id and session_id not in jokes_data["shared_references"][ref_key]["contexts"]:
                    jokes_data["shared_references"][ref_key]["contexts"].append(session_id)

        # Save back via DataStore
        self.ds.set_jokes_and_references(jokes_data)

    def get_jokes_and_references(self):
        """Get all stored jokes and references."""
        return self.ds.get_jokes_and_references()

    def get_callback_material(self, current_topic=None, limit=3):
        """Get relevant callback material for the current conversation."""
        jokes_data = self.get_jokes_and_references()

        callbacks = []

        # Get most frequently mentioned recurring topics
        topics = sorted(
            jokes_data.get("recurring_topics", {}).items(),
            key=lambda x: x[1].get("mention_count", 0),
            reverse=True
        )[:limit]

        for _topic_key, topic_data in topics:
            if topic_data.get("mention_count", 0) >= 2:  # Only if mentioned multiple times
                callbacks.append({
                    "type": "recurring_topic",
                    "text": f"Like we talked about with {topic_data['text']}...",
                    "context": topic_data,
                    "weight": topic_data.get("mention_count", 0)
                })

        # Get shared references
        refs = jokes_data.get("shared_references", {})
        for _ref_key, ref_data in list(refs.items())[:limit]:
            if ref_data.get("mention_count", 0) >= 1:
                callbacks.append({
                    "type": "shared_reference",
                    "text": f"Like that thing you mentioned about '{ref_data['text']}'",
                    "context": ref_data,
                    "weight": ref_data.get("mention_count", 0)
                })

        # Sort by weight (mention count)
        callbacks.sort(key=lambda x: x["weight"], reverse=True)

        return callbacks[:limit]


# Singleton instance
joke_tracker = JokeTracker()
