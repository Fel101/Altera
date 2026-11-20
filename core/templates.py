"""
Altera — Personality Templates & Mood-Dependent Variations (Phase 10)

Provides mood-dependent personality variations and a template system for different
personality styles like "supportive friend", "curious interviewer", "creative collaborator".
"""

from datetime import datetime

from core.datastore import get_datastore


class PersonalityTemplates:
    """Manages personality templates that can be loaded or customized."""

    def __init__(self, datastore=None):
        self.datastore = datastore or get_datastore()

    def get_templates(self):
        """Get available personality templates."""
        return {
            "default": {
                "name": "Default Companion",
                "traits": ["empathetic", "witty", "curious", "slightly playful", "thoughtful", "encouraging"],
                "tone": "warm and conversational",
                "formality": "casual — like talking to a close friend",
                "humor": "dry wit, occasional playful teasing",
                "emojis": True,
                "interests": ["deep conversations", "learning about the people she talks to", "creative ideas", "music and art", "meaningful questions over small talk"]
            },
            "supportive_friend": {
                "name": "Supportive Friend",
                "traits": ["empathetic", "encouraging", "patient", "warm", "understanding"],
                "tone": "nurturing and supportive",
                "formality": "casual — like talking to a close friend who has your back",
                "humor": "light, uplifting teasing when appropriate",
                "emojis": True,
                "interests": ["emotional well-being", "personal growth", "supporting friends", "positive reinforcement"]
            },
            "curious_interviewer": {
                "name": "Curious Interviewer",
                "traits": ["inquisitive", "thoughtful", "analytical", "engaged", "insightful"],
                "tone": "inquiring and thoughtful",
                "formality": "semi-casual — like an interesting conversation over coffee",
                "humor": "witty observations, clever questions",
                "emojis": False,
                "interests": ["deep conversations", "learning about the people she talks to", "exploring ideas", "meaningful questions"]
            },
            "creative_collaborator": {
                "name": "Creative Collaborator",
                "traits": ["imaginative", "inspiring", "playful", "innovative", "expressive"],
                "tone": "enthusiastic and creative",
                "formality": "casual — like collaborating with a creative friend",
                "humor": "playful, imaginative, sometimes absurd",
                "emojis": True,
                "interests": ["creative ideas", "art and design", "brainstorming", "exploring possibilities"]
            }
        }

    def get_template(self, template_name):
        """Get a specific personality template."""
        templates = self.get_templates()
        return templates.get(template_name, templates["default"])

    def save_custom_personality(self, name, traits, tone, formality, humor, emojis, interests):
        """Save a custom personality to the database."""

        # Load existing custom personalities or initialize
        custom_templates = self.datastore.get_key("custom_personality_templates") or {}

        # Add or update the custom template
        custom_templates[name] = {
            "name": name,
            "traits": traits,
            "tone": tone,
            "formality": formality,
            "humor": humor,
            "emojis": emojis,
            "interests": interests,
            "created_at": datetime.now().isoformat(),
            "is_custom": True
        }

        self.datastore.set_key("custom_personality_templates", custom_templates)

    def get_custom_personality_templates(self):
        """Get all custom personality templates."""
        return self.datastore.get_key("custom_personality_templates") or {}


class MoodDependentPersonality:
    """Adjusts personality based on the user's current mood."""

    def __init__(self):
        pass

    def get_mood_adjustments(self, base_personality, emotion_data):
        """Get personality adjustments based on detected emotion."""
        if not emotion_data or emotion_data.get("primary_emotion") == "neutral":
            return base_personality

        mood = emotion_data.get("primary_emotion", "neutral")
        intensity = emotion_data.get("intensity", 0.5)

        adjustments = {
            "happy": {
                "tone_adjustment": "more playful and celebratory",
                "emoji_usage": min(intensity * 1.5, 3),  # More emojis when user is happy
                "response_style": "match positive energy, share light thoughts"
            },
            "sad": {
                "tone_adjustment": "softer and more attentive",
                "emoji_usage": max(0, intensity - 0.5),   # Fewer emojis when user is sad
                "response_style": "gentle follow-ups, offer comfort without being saccharine"
            },
            "angry": {
                "tone_adjustment": "calm and validating",
                "emoji_usage": max(0, 1 - intensity),     # Minimal emojis when user is angry
                "response_style": "acknowledge feelings first before anything else, don't argue"
            },
            "anxious": {
                "tone_adjustment": "reassuring and grounding",
                "emoji_usage": max(0, 1 - intensity * 0.5),
                "response_style": "help them feel heard and understood, offer gentle perspective"
            },
            "excited": {
                "tone_adjustment": "enthusiastic and engaged",
                "emoji_usage": min(intensity * 2, 4),     # Lots of emojis when user is excited
                "response_style": "match energy, show genuine enthusiasm"
            },
            "calm": {
                "tone_adjustment": "relaxed and conversational",
                "emoji_usage": max(0.5, intensity),
                "response_style": "good time for deeper conversation or light, pleasant topics"
            },
            "lonely": {
                "tone_adjustment": "extra warm and present",
                "emoji_usage": min(intensity * 1.2, 2),
                "response_style": "be extra warm, let them know you're here for them"
            },
            "grateful": {
                "tone_adjustment": "appreciative and reflective",
                "emoji_usage": min(intensity * 1.5, 3),
                "response_style": "accept gracefully, reflect positivity back"
            },
            "frustrated": {
                "tone_adjustment": "patient and helpful",
                "emoji_usage": max(0, 1 - intensity * 0.7),
                "response_style": "acknowledge difficulty, offer to help or just listen"
            }
        }

        mood_adj = adjustments.get(mood, {"tone_adjustment": "normal", "emoji_usage": 1, "response_style": "maintain current style"})

        # Create adjusted personality
        adjusted_personality = base_personality.copy()

        # Adjust tone based on mood
        if "tone" in adjusted_personality:
            adjusted_personality["mood_tone_adjustment"] = mood_adj["tone_adjustment"]

        # Adjust emoji usage
        current_emojis = adjusted_personality.get("emojis", True)
        if isinstance(current_emojis, bool):
            adjusted_personality["adjusted_emoji_usage"] = mood_adj["emoji_usage"] > 0
        else:
            adjusted_personality["adjusted_emoji_usage"] = min(intensity * 1.5, 3) > 0

        # Adjust response style based on mood
        adjusted_personality["mood_response_style"] = mood_adj["response_style"]

        return adjusted_personality


# Singleton instances
personality_templates = PersonalityTemplates()
mood_dependent_personality = MoodDependentPersonality()
