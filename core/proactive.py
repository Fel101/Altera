"""Proactive Engagement System — scheduled check-ins, follow-up tracking, and proactive messages.

Handles:
- Tracking things the user mentioned they'd do (follow-ups)
- Scheduled check-in messages based on time since last interaction
- LLM-generated natural-sounding check-ins using context (follow-ups, mood, recent topics)

Storage: Follow-up items stored as JSON in SQLite `user_profile` table under key `follow_ups`.
"""

import json
from datetime import datetime, UTC

from core.datastore import get_datastore
from core.llm import get_client
from core.profile import proactive_settings


def _hours_since(stamp, utc: bool):
    """Hours since a timestamp, or None. SQLite stamps ('2026-09-21 05:17:50') are UTC; the ones this module writes are local time."""
    if not stamp:
        return None
    try:
        then = datetime.fromisoformat(str(stamp).replace("Z", ""))
        now = datetime.now(UTC).replace(tzinfo=None) if utc else datetime.now()
        return (now - then).total_seconds() / 3600
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# FollowUpTracker — tracks things the user mentioned they'd do
# ---------------------------------------------------------------------------

class FollowUpTracker:
    """Tracks pending follow-up items extracted from conversations."""

    def __init__(self, datastore=None):
        self.datastore = datastore or get_datastore()

    def extract_follow_ups(self, text: str) -> list[dict]:
        """Use LLM to extract potential follow-ups from message text.

        Returns a list of dicts with keys: item, context, priority (high/medium/low).
        """
        prompt = (
            "You are analyzing a user's message for things they mentioned they would do in the future. "
            "Extract any follow-up items — commitments, plans, appointments, goals, or intentions. "
            "Return ONLY a JSON array of objects with these fields:\n"
            "- item: short description (e.g., 'Doctor appointment')\n"
            "- context: fuller context (e.g., 'User has a doctor visit scheduled for tomorrow at 3pm')\n"
            "- priority: one of 'high', 'medium', or 'low'\n\n"
            "If no follow-ups are found, return an empty array [].\n\n"
            f"Message to analyze:\n{text}"
        )

        client = get_client()
        messages = [
            {"role": "system", "content": "You extract structured data. Return only valid JSON."},
            {"role": "user", "content": prompt}
        ]
        response = client.chat(messages)

        try:
            # Extract JSON from possible markdown code blocks
            content = response.strip()
            if "```" in content:
                for block in content.split("```"):
                    if block.strip().startswith("json"):
                        content = block.strip()[4:].strip()
                        break
                    elif block.strip():
                        content = block.strip()
                        break

            items = json.loads(content)
            if not isinstance(items, list):
                return []

            # Normalize each item
            result = []
            for item in items:
                if isinstance(item, dict) and "item" in item:
                    result.append({
                        "item": str(item.get("item", "")),
                        "context": str(item.get("context", "")),
                        "priority": str(item.get("priority", "medium")).lower(),
                        "created_at": datetime.now().isoformat(),
                        "status": "pending"
                    })
            return result

        except (json.JSONDecodeError, AttributeError):
            return []

    def get_pending_followups(self, limit: int = 5) -> list[dict]:
        """Return pending follow-up items sorted by priority then recency.

        Priority order: high > medium > low. Within same priority, newest first.
        """
        profile = self.datastore.get_user_profile()
        follow_ups = profile.get("follow_ups", [])

        if not follow_ups:
            return []

        # Filter pending and sort
        pending = [fu for fu in follow_ups if fu.get("status") == "pending"]

        priority_order = {"high": 0, "medium": 1, "low": 2}
        pending.sort(key=lambda x: (
            priority_order.get(x.get("priority", "medium"), 1),
            -datetime.fromisoformat(x["created_at"]).timestamp() if "created_at" in x else 0
        ))

        return pending[:limit]

    def mark_followup_done(self, index: int) -> bool:
        """Mark a follow-up item as completed by its position in the list."""
        profile = self.datastore.get_user_profile()
        follow_ups = profile.get("follow_ups", [])

        if 0 <= index < len(follow_ups):
            follow_ups[index]["status"] = "done"
            follow_ups[index]["completed_at"] = datetime.now().isoformat()
            self.datastore.set_user_profile({"follow_ups": follow_ups})
            return True

        return False

    def add_followup(self, item: str, context: str = "", priority: str = "medium") -> bool:
        """Add a new follow-up item."""
        profile = self.datastore.get_user_profile()
        follow_ups = profile.get("follow_ups", [])

        follow_ups.append({
            "item": item,
            "context": context,
            "priority": priority.lower(),
            "created_at": datetime.now().isoformat(),
            "status": "pending"
        })

        self.datastore.set_user_profile({"follow_ups": follow_ups})
        return True


# ---------------------------------------------------------------------------
# ProactiveManager — orchestrates proactive engagement
# ---------------------------------------------------------------------------

class ProactiveManager:
    """Orchestrates proactive check-in messages based on context."""

    def __init__(self, datastore=None):
        self.datastore = datastore or get_datastore()
        self.followup_tracker = FollowUpTracker(self.datastore)

    def should_proactively_engage(self, last_interaction_hours: int = 4, session_id: int | None = None) -> bool:
        """True when this chat has been quiet for at least this many hours and no check-in was made in that time."""
        session_id = session_id or self.datastore.get_last_active_session()
        if not session_id:
            return False
        quiet = _hours_since(self.datastore.last_message_at(session_id), utc=True)
        if quiet is None:                      # nothing has been said yet: nothing to check in about
            return False
        last_checkin = (self.datastore.get_key("last_proactive_message") or {}).get("generated_at")
        since_checkin = _hours_since(last_checkin, utc=False)
        return quiet >= last_interaction_hours and (since_checkin is None or since_checkin >= last_interaction_hours)

    def get_proactive_message(self, session_id: int | None = None) -> str | None:
        """Generate a natural-sounding check-in message using LLM with context.

        Uses follow-ups, mood trend, and recent conversation topics as context.
        Returns None if no message should be generated.
        """
        from core.knowledge import floor, prompt_messages
        session_id = session_id or self.datastore.get_last_active_session()
        # Check-ins obey the same owner recall boundary as normal chat. Visible
        # history can remain on disk after Forget without becoming model context.
        if session_id:
            records = prompt_messages(self.datastore, session_id, self.datastore.get_messages(session_id, limit=6))
            messages = [{"role": m.role, "content": m.content} for m in records]
        else:
            messages = []
            for message in self.datastore.get_recent_messages_across_sessions(limit=20):
                owner = self.datastore.get_session(message['session_id'])
                if owner and message['id'] > floor(self.datastore, owner.avatar_id or ''):
                    messages.append(message)
        if not messages:
            return None

        # Legacy shared follow-ups and moods have no source IDs. Keep them out of
        # scoped chats and suppress them after any recall reset/source invalidation.
        shared_context = not session_id and not any(
            entry.key.startswith('knowledge_recall:') or
            (entry.key.startswith('knowledge_dirty') and isinstance(entry.value, dict) and entry.value.get('pending'))
            for entry in self.datastore.get_all_keys())
        pending_followups = self.followup_tracker.get_pending_followups(limit=5) if shared_context else []
        profile = self.datastore.get_user_profile() if shared_context else {}
        emotion_data = profile.get("emotion_history", [])
        recent_moods = [e.get("category", "neutral") for e in emotion_data[-10:]] if emotion_data else []
        recent_context = ""
        if messages:
            recent_context = "\n".join([
                f"{'User' if m.get('role') == 'user' else 'Assistant'}: {m.get('content', '')}"
                for m in messages[-5:]
            ])

        # Build context summary for LLM
        followup_summary = ""
        if pending_followups:
            items = "\n".join([
                f"- [{fu['priority']}] {fu['item']} ({fu.get('context', '')})"
                for fu in pending_followups
            ])
            followup_summary = (
                f"\n\nPending follow-ups:\n{items}\n\n"
                "Reference these naturally if relevant — don't list them mechanically."
            )

        mood_summary = ""
        if recent_moods:
            from core.emotion import analyze_emotion_trend
            trend = analyze_emotion_trend(recent_moods)
            mood_summary = f"\n\nRecent mood trend: {trend}"

        prompt = (
            "You are generating a natural, caring check-in message to the user. "
            "Be warm and conversational — like a friend checking in, not a bot reading data.\n"
            "\n"
            f"{followup_summary}{mood_summary}\n"
            "\n"
            "Recent conversation context:\n"
            f"{recent_context[:500] if recent_context else '(No recent conversations)'}\n"
            "\n"
            "Guidelines:\n"
            "- Keep it brief (1-2 sentences)\n"
            "- Reference specific details from the follow-ups or context when relevant\n"
            "- Use a warm, natural tone — include an emoji if it fits naturally\n"
            "- If nothing particularly interesting to check on, ask something open-ended\n"
            "- NEVER say 'based on my analysis' or reference data/metrics directly\n"
            "\n"
            "Generate the check-in message:"
        )

        client = get_client()
        llm_messages = [
            {"role": "system", "content": "You generate warm, natural messages. Return only the message text."},
            {"role": "user", "content": prompt}
        ]
        response = client.chat(llm_messages)

        # Clean up the response
        message = response.strip()
        if message.startswith('"') and message.endswith('"'):
            message = message[1:-1]

        return message if message else None

    def check_and_generate(self, session_id: int | None = None) -> dict:
        """Full proactive check-in flow: decide → generate → store → return.

        Returns a dict with keys:
        - message: the generated message (or null)
        - type: 'proactive_checkin' or 'none'
        - has_followups: whether pending follow-ups exist
        - reason: why no check-in was generated (if applicable)
        """
        # Check if we should engage. The switch and the interval are the ones saved from Settings (core/profile.py).
        settings = proactive_settings()
        enabled, interval_hours = settings["enabled"], settings["interval_hours"]

        if not enabled:
            pending = self.followup_tracker.get_pending_followups(limit=5)
            return {
                "message": None,
                "type": "none",
                "has_followups": len(pending) > 0,
                "reason": "Proactive check-ins are disabled"
            }

        if not self.should_proactively_engage(interval_hours, session_id):
            pending = self.followup_tracker.get_pending_followups(limit=5)
            return {
                "message": None,
                "type": "none",
                "has_followups": len(pending) > 0,
                "reason": "No check-in needed right now"
            }

        if session_id and not self.datastore.count_user_messages(session_id):
            return {"message": None, "type": "none", "has_followups": False, "reason": "She has not met the person yet"}

        # Generate message
        message = self.get_proactive_message(session_id)

        if not message:
            pending = self.followup_tracker.get_pending_followups(limit=5)
            return {
                "message": None,
                "type": "none",
                "has_followups": len(pending) > 0,
                "reason": "No check-in generated"
            }

        # Store the proactive message for display
        self.datastore.set_user_profile({
            "last_proactive_message": {
                "message": message,
                "generated_at": datetime.now().isoformat()
            }
        })

        pending = self.followup_tracker.get_pending_followups(limit=5)
        return {
            "message": message,
            "type": "proactive_checkin",
            "has_followups": len(pending) > 0
        }


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

proactive_manager = ProactiveManager()
