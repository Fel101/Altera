"""
Altera — Life Event Timeline (Phase 8)

Extracts significant life events from conversations: birthdays, achievements,
trips, milestones. Builds a chronological timeline of the user's life as Altera knows it.
"""

import logging
import json
import re
from datetime import datetime
from core.constants import BASE_CONFIDENCE_SCORES, LLM_TEMPERATURE_ANALYSIS, LLM_MAX_TOKENS_LONG
from core.datastore import get_datastore

LOGGER = logging.getLogger(__name__)


# Patterns for detecting life events in conversation text
EVENT_PATTERNS = {
    "birthday": [
        r"(?:my|your|his|her|their) birthday(?:\s+(?:is|was|falls?))?\s*:?\s*(?:on\s+)?(\w+\s+\d+|\d+\s+\w+)",
        r"i (?:turned|am turning)\s+(\d+)",
        r"(?:happy|celebrating) birthday",
    ],
    "achievement": [
        r"(?:just got|just finished|just started|got|finished|started)\s+(a |an |the )?(graduation|degree|promotion|award|certification|certificate|milestone)",
        r"i (?:got|received|earned|completed) (a |an |the )?(\w+)",
        r"(?:congratulations|congrats)\s+(to|on)",
        r"(?:won|winning|wins?)\s+(a |an |the )?(\w+)",
    ],
    "trip": [
        r"(?:going to|went to|traveled to|visiting|visited) ([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*(?:,?\s+(?:USA|US|UK|Canada|Australia|Japan|France|Germany|Italy|Spain|Mexico|Brazil|India|China))?)",
        r"(?:back from|returning from) ([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)",
        r"i (?:am planning|planned|plan) to go to ([A-Z][a-z]+)",
    ],
    "work": [
        r"(?:started a new job|new job|new position|new role)\s+(?:at|with|for)?\s*(.*)",
        r"i (?:got|hired|landed) a job at (.*)",
        r"(?:promoted to|promoted as) (\w+)",
    ],
    "relationship": [
        r"(?:started dating|began dating|got together with) ([A-Z][a-z]+)",
        r"i (?:met|met up with) someone named? ([A-Z][a-z]+)",
        r"(?:breaking up|breakup|splitting up) with ([A-Z][a-z]+)",
    ],
    "health": [
        r"(?:went to the doctor|had a checkup|got checked) (?:and|who)?\s*(.*)",
        r"i (?:have|had|was diagnosed with) (\w+)",
        r"(?:feeling|feel) (better|worse|good|bad|sick|tired|exhausted)",
    ],
    "holiday": [
        r"(?:christmas|thanksgiving|halloween|easter|new year|valentine's day)\s*(.*)",
        r"celebrating?\s+(?:christmas|thanksgiving|halloween|easter|new year|valentine's)",
    ],
    "general_milestone": [
        r"(?:big news|important thing|something happened|guess what)",
        r"(?:finally finished|at last completed|just wrapped up) (.*)",
        r"(?:excited about|looking forward to|can't wait for) (.*)",
    ],
}


class LifeEventExtractor:
    """Extract significant life events from conversation text."""

    def __init__(self):
        self.events = []  # List of extracted event dicts

    def extract_from_text(self, text, session_id=None, timestamp=None, use_llm=False):
        """Extract all life events from a piece of text. Returns list of event dicts.

        Patterns only, unless use_llm is True: then the chat model reads the text too (one model call per text). Off by default
        because the callers go through many messages at a time (the timeline and extract routes take the last 20 to 100)."""
        events_found = []
        seen_keys = set()

        for event_type, patterns in EVENT_PATTERNS.items():
            for pattern in patterns:
                matches = re.finditer(pattern, text, re.IGNORECASE)
                for match in matches:
                    groups = match.groups()
                    detail = groups[-1].strip() if groups else ""
                    raw = text[match.start():match.end()]

                    # Avoid exact duplicates within same extraction
                    dedup_key = (event_type, detail.lower())
                    if dedup_key in seen_keys:
                        continue
                    seen_keys.add(dedup_key)

                    event = {
                        "type": event_type,
                        "detail": detail or raw,
                        "confidence": self._calculate_confidence(event_type, match),
                        "timestamp": timestamp or datetime.now().isoformat(),
                        "session_id": session_id,
                        "raw_text": raw,
                    }
                    events_found.append(event)

        # The chat model can read it too, for richer events, when the caller asks for it
        llm_events = self._extract_with_llm(text, session_id) if use_llm else []
        if llm_events:
            existing_keys = {(e["type"], e.get("detail", "").lower()) for e in events_found}
            for le in llm_events:
                key = (le["type"], le.get("detail", "").lower())
                if key not in existing_keys:
                    le["timestamp"] = timestamp or datetime.now().isoformat()
                    le["session_id"] = session_id
                    events_found.append(le)

        self.events.extend(events_found)
        return events_found

    def _calculate_confidence(self, event_type, match):
        """Calculate confidence score for a pattern-based extraction."""
        base = BASE_CONFIDENCE_SCORES.get(event_type, 0.5)

        if event_type in ("birthday", "achievement"):
            base += 0.1
        if match.end() - match.start() > 30:
            base += 0.05

        return min(base, 0.98)

    def _extract_with_llm(self, text, session_id=None):
        """Use LLM to extract richer event information."""
        try:
            from core.llm import ask

            prompt = f"""Analyze this message and extract any significant life events. Return ONLY a JSON array with no markdown or extra text. If no events found, return an empty array [].

Message: "{text}"

Return exactly this JSON structure (no markdown, no code blocks):
[{{"type": "birthday|achievement|trip|work|relationship|health|holiday|milestone", "detail": "brief description", "confidence": 0.85}}]

Rules:
- Only extract real events the user is describing about themselves
- type should be one of: birthday, achievement, trip, work, relationship, health, holiday, milestone
- detail should be a brief summary (under 100 chars)
- confidence between 0.3 and 0.95"""

            content = ask("You are a life event extraction engine. Return ONLY valid JSON.", prompt,
                          temperature=LLM_TEMPERATURE_ANALYSIS, max_tokens=LLM_MAX_TOKENS_LONG)

            if content.startswith("```"):
                lines = [line for line in content.split("\n") if not line.strip().startswith("```")]
                content = "\n".join(lines)

            events = json.loads(content)
            return events if isinstance(events, list) else []

        except Exception:
            LOGGER.exception("LLM extraction failed")
            return []


class EventTimeline:
    """Maintains a chronological timeline of life events."""

    def __init__(self):
        self.ds = get_datastore()
        self.extractor = LifeEventExtractor()
        # Table creation is handled by core.datastore.init_db() — no need to duplicate

    def add_events_from_conversation(self, text, session_id=None, use_llm=False):
        """Extract and store events from a conversation message (patterns only, unless use_llm: see extract_from_text)."""
        events = self.extractor.extract_from_text(text, session_id, use_llm=use_llm)

        if not events:
            return []

        for event in events:
            self.ds.add_life_event(
                session_id=session_id,
                event_type=event["type"],
                detail=event["detail"][:500],
                confidence=event["confidence"],
                raw_text=event.get("raw_text", "")[:200],
            )

        return events

    def get_timeline(self, limit=10):
        """Get the life event timeline, sorted chronologically."""
        events = self.ds.get_events(limit=limit)
        # Convert LifeEvent objects to dicts for backward compatibility
        result = [dict(e._asdict()) if hasattr(e, '_asdict') else {k: v for k, v in e.__dict__.items()} for e in events]
        result.sort(key=lambda e: e.get("timestamp", ""))
        return result

    def get_events_by_type(self, event_type=None):
        """Get events filtered by type."""
        if event_type:
            events = self.ds.get_events_by_type(event_type)
        else:
            events = self.ds.get_events(limit=100)
        # Convert LifeEvent objects to dicts for backward compatibility
        return [dict(e._asdict()) if hasattr(e, '_asdict') else {k: v for k, v in e.__dict__.items()} for e in events]

    def get_summary(self):
        """Get a summary of all tracked events by type."""
        raw = self.ds.get_event_summary()
        # Convert latest_date/earliest_date keys to match old format (last_seen/first_seen)
        return {
            k: {"count": v["count"], "last_seen": v["latest_date"], "first_seen": v["earliest_date"]}
            for k, v in raw.items()
        }


# Singleton instance
event_timeline = EventTimeline()
