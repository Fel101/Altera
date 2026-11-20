"""
Altera — Relationship Graph (Phase 4)

Tracks people, places, and events mentioned in conversations to build a
relationship graph that makes Altera's responses more personal and contextual.
"""

import logging
import json
from datetime import datetime
from core.constants import DEPTH_LEVELS, DEPTH_SCORE_MAX, LLM_TEMPERATURE_ANALYSIS
from core.datastore import get_datastore

LOGGER = logging.getLogger(__name__)


class RelationshipGraph:
    """Maintains a graph of relationships between entities the user mentions."""

    def __init__(self):
        self.ds = get_datastore()

    def extract_entities(self, text, session_id=None):
        """Extract people, places, and organizations from text using LLM analysis."""
        prompt = f"""Analyze this message and extract named entities. Return ONLY a JSON object with no markdown or extra text:

Message: "{text}"

Return exactly this JSON structure (no markdown, no code blocks):
{{"people": ["name1", "name2"], "places": ["place1"], "organizations": ["org1"]}}

Rules:
- Only extract real named entities (not generic words like 'work' or 'home')
- People should be names of actual people the user mentions
- Places should be cities, countries, buildings, etc.
- Organizations should be companies, schools, institutions"""

        try:
            from core.llm import ask

            content = ask("You are an entity extraction engine. Return ONLY valid JSON.", prompt,
                          temperature=LLM_TEMPERATURE_ANALYSIS, max_tokens=250)

            # Clean markdown wrapping
            if content.startswith("```"):
                lines = [line for line in content.split("\n") if not line.strip().startswith("```")]
                content = "\n".join(lines)

            result = json.loads(content)

            # Validate structure
            result.setdefault("people", [])
            result.setdefault("places", [])
            result.setdefault("organizations", [])

            # Store in relationship graph
            if session_id:
                self._store_entities(result, session_id)

            return result

        except Exception:
            LOGGER.exception("Entity extraction failed")
            return {"people": [], "places": [], "organizations": []}

    def _store_entities(self, entities, session_id):
        """Store extracted entities in the database."""
        graph = self.ds.get_relationship_graph()

        # Add people with metadata
        for person in entities.get("people", []):
            person_lower = person.lower().strip()
            if person_lower not in graph:
                graph[person_lower] = {
                    "name": person.strip(),
                    "type": "person",
                    "first_mentioned": datetime.now().isoformat(),
                    "mention_count": 1,
                    "context": [],
                    "relationships": {}
                }
            else:
                graph[person_lower]["mention_count"] += 1
                if session_id:
                    graph[person_lower]["last_seen"] = datetime.now().isoformat()

        # Add places with metadata
        for place in entities.get("places", []):
            place_lower = place.lower().strip()
            if place_lower not in graph:
                graph[place_lower] = {
                    "name": place.strip(),
                    "type": "place",
                    "first_mentioned": datetime.now().isoformat(),
                    "mention_count": 1,
                    "context": []
                }
            else:
                graph[place_lower]["mention_count"] += 1

        # Add organizations with metadata
        for org in entities.get("organizations", []):
            org_lower = org.lower().strip()
            if org_lower not in graph:
                graph[org_lower] = {
                    "name": org.strip(),
                    "type": "organization",
                    "first_mentioned": datetime.now().isoformat(),
                    "mention_count": 1,
                    "context": []
                }
            else:
                graph[org_lower]["mention_count"] += 1

        # Save back via DataStore
        self.ds.set_relationship_graph(graph)

    def get_graph(self):
        """Get the full relationship graph."""
        return self.ds.get_relationship_graph()

    def get_people(self):
        """Get all people in the graph."""
        graph = self.get_graph()
        return {k: v for k, v in graph.items() if v.get("type") == "person"}

    def get_places(self):
        """Get all places in the graph."""
        graph = self.get_graph()
        return {k: v for k, v in graph.items() if v.get("type") == "place"}

    def search_by_name(self, query):
        """Search entities by name (case-insensitive)."""
        graph = self.get_graph()
        results = {}
        query_lower = query.lower()

        for key, data in graph.items():
            if query_lower in key.lower() or query_lower in data.get("name", "").lower():
                results[key] = data

        return results

    def update_depth_scores(self):
        """Recalculate relationship depth scores based on mention frequency and recency.

        Depth scoring:
        - Base score from mention_count (more mentions = deeper)
        - Recency bonus for recent interactions
        - Context richness bonus (more unique contexts = deeper)
        - Returns updated graph with 'depth_score' field

        Depth levels:
        0-2:   Acquaintance (barely know them)
        2-4:   Casual (know of them, some context)
        4-6:   Close (regular mentions, good context)
        6-8:   Very close (frequent mentions, rich context)
        8-10:  Core (most important people/places in user's life)
        """
        graph = self.get_graph()
        now = datetime.now()

        for data in graph.values():
            mention_count = data.get("mention_count", 0)

            # Base score from mentions (log scale to prevent runaway numbers)
            depth_score = min(mention_count * 0.5, DEPTH_LEVELS["close"])

            # Recency bonus (0-2 points)
            last_seen = data.get("last_seen") or data.get("first_mentioned")
            if last_seen:
                try:
                    last_dt = datetime.fromisoformat(last_seen.replace('Z', '+00:00').replace('+00:00', ''))
                    days_ago = (now - last_dt).days
                    if days_ago < 7:
                        depth_score += 2.0  # Very recent
                    elif days_ago < 30:
                        depth_score += 1.5  # Recent
                    elif days_ago < 90:
                        depth_score += 0.8  # Somewhat recent
                    else:
                        depth_score += 0.2  # Distant memory
                except (ValueError, TypeError):
                    pass

            # Context richness bonus (0-1 point)
            contexts = data.get("context", [])
            if isinstance(contexts, list):
                unique_contexts = len(set(c.lower() for c in contexts))
                depth_score += min(unique_contexts * 0.2, 1.0)

            # Cap at 10
            depth_score = min(depth_score, DEPTH_SCORE_MAX)

            # Assign depth level label
            if depth_score >= 8:
                depth_level = "core"
            elif depth_score >= 6:
                depth_level = "very_close"
            elif depth_score >= 4:
                depth_level = "close"
            elif depth_score >= 2:
                depth_level = "casual"
            else:
                depth_level = "acquaintance"

            data["depth_score"] = round(depth_score, 1)
            data["depth_level"] = depth_level

        # Save updated graph back to DB via DataStore
        self.ds.set_relationship_graph(graph)

        return graph

    def get_closest_relationships(self, entity_type=None, min_depth=2.0):
        """Get relationships sorted by depth score (closest first)."""
        graph = self.update_depth_scores()

        results = {}
        for key, data in graph.items():
            if entity_type and data.get("type") != entity_type:
                continue
            if data.get("depth_score", 0) >= min_depth:
                results[key] = data

        # Sort by depth score descending
        sorted_results = dict(
            sorted(results.items(), key=lambda x: x[1].get("depth_score", 0), reverse=True)
        )
        return sorted_results

    def get_relationship_insights(self):
        """Get insights about relationship patterns for proactive behavior.

        Returns suggestions like:
        - "You haven't mentioned Sarah in a while"
        - "Your relationship with X has grown stronger this month"
        - "New person Y appeared in your conversations"
        """
        graph = self.update_depth_scores()
        insights = []

        now = datetime.now()
        people = {k: v for k, v in graph.items() if v.get("type") == "person"}

        # Check for fading relationships (people who were once close but haven't been mentioned)
        for name, data in people.items():
            last_seen = data.get("last_seen") or data.get("first_mentioned")
            if not last_seen:
                continue

            try:
                last_dt = datetime.fromisoformat(last_seen.replace('Z', '+00:00').replace('+00:00', ''))
                days_ago = (now - last_dt).days

                # If someone was mentioned before but hasn't been in a while
                if data.get("mention_count", 0) > 2 and days_ago > 30:
                    insights.append({
                        "type": "fading_relationship",
                        "message": f"You haven't mentioned {data['name']} in {days_ago} days — is everything okay?",
                        "entity": name,
                        "priority": "medium" if days_ago < 60 else "high"
                    })
            except (ValueError, TypeError):
                pass

        # Check for new relationships
        for name, data in people.items():
            first_mentioned = data.get("first_mentioned")
            if not first_mentioned:
                continue

            try:
                first_dt = datetime.fromisoformat(first_mentioned.replace('Z', '+00:00').replace('+00:00', ''))
                days_since = (now - first_dt).days

                # New person mentioned recently with growing importance
                if days_since < 14 and data.get("mention_count", 0) >= 2:
                    insights.append({
                        "type": "new_relationship",
                        "message": f"You've been talking about {data['name']} a lot lately — they seem important to you.",
                        "entity": name,
                        "priority": "low"
                    })
            except (ValueError, TypeError):
                pass

        # Check for deepening relationships
        for name, data in people.items():
            if data.get("depth_level") == "core" and data.get("mention_count", 0) > 5:
                insights.append({
                    "type": "deepening_relationship",
                    "message": f"{data['name']} is one of the most important people in your life — mentioned {data['mention_count']} times.",
                    "entity": name,
                    "priority": "info"
                })

        return insights


# ============================================================
# Personality evolution tracking
# ============================================================

class PersonalityEvolution:
    """Tracks how the user's communication style evolves over time."""

    def __init__(self):
        self.ds = get_datastore()

    def analyze_communication_style(self, session_id=None):
        """Analyze the user's current communication style using LLM."""
        # Use DataStore to get recent user messages instead of raw SQL
        # The chat asked about, or with none given the one that was open last: its newest 10 messages, oldest first
        session_id = session_id or self.ds.get_last_active_session()
        messages = list(reversed(self.ds.get_user_messages(session_id, limit=10))) if session_id else []

        if not messages:
            return None

        combined_text = "\n".join(messages)

        try:
            from core.llm import ask

            prompt = f"""Analyze the communication style of these recent messages and return ONLY a JSON object with no markdown or extra text:

{combined_text}

Return exactly this JSON structure (no markdown, no code blocks):
{{"formality": "casual|neutral|formal", "tone": "warm|direct|playful|serious|thoughtful",
"length_preference": "brief|moderate|detailed", "humor_level": "low|medium|high",
"key_patterns": ["pattern1", "pattern2"], "language_features": ["feature1"]}}"""

            content = ask("You are a communication style analyzer. Return ONLY valid JSON.", prompt,
                          temperature=LLM_TEMPERATURE_ANALYSIS, max_tokens=250)

            if content.startswith("```"):
                lines = [line for line in content.split("\n") if not line.strip().startswith("```")]
                content = "\n".join(lines)

            return json.loads(content)

        except Exception:
            LOGGER.exception("Style analysis failed")
            return None

    def get_style_profile(self):
        """Get the stored communication style profile."""
        return self.ds.get_communication_style()

    def update_style_profile(self, style_data):
        """Update the communication style profile."""
        existing = self.get_style_profile() or {}

        # Merge with new data (keep older timestamps for first_seen)
        merged = {**existing, **style_data}
        if "first_analyzed" not in merged:
            merged["first_analyzed"] = datetime.now().isoformat()
        merged["last_updated"] = datetime.now().isoformat()

        self.ds.set_communication_style(merged)


# ============================================================
# Singleton instances
# ============================================================

relationship_graph = RelationshipGraph()
personality_evolution = PersonalityEvolution()
