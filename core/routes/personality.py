"""Personality evolution and mood adjustment routes extracted from admin.py (Session 6: Blueprints).

Routes for communication style analysis, personality templates, custom personalities,
and mood-dependent adjustments.
"""

import logging
from flask import Blueprint, request, jsonify
from core.constants import api_error

LOGGER = logging.getLogger(__name__)

bp = Blueprint('personality', __name__)


# ============================================================
# Routes — Personality Evolution & Templates
# ============================================================

@bp.route("/api/personality/style", methods=["GET"])
def get_communication_style():
    """Get the user's communication style profile."""
    try:
        from core.relationships import personality_evolution

        style = personality_evolution.get_style_profile()

        if not style:
            style = personality_evolution.analyze_communication_style()
            if style:
                personality_evolution.update_style_profile(style)

        return jsonify(style or {})
    except Exception:
        LOGGER.exception("Style fetch failed")
        return jsonify({})


@bp.route("/api/personality/style/analyze", methods=["POST"])
def analyze_communication_style():
    """Force re-analysis of communication style."""
    try:
        from core.relationships import personality_evolution

        style = personality_evolution.analyze_communication_style()

        if style:
            personality_evolution.update_style_profile(style)
            return jsonify({"status": "ok", "style": style})
        else:
            return jsonify({"error": "No messages to analyze"}), 400
    except Exception:
        LOGGER.exception("Style analysis failed")
        return api_error("internal_error", 500)


@bp.route("/api/personality/evolution", methods=["GET"])
def get_personality_evolution():
    """Get personality evolution data."""
    try:
        from core.relationships import personality_evolution, relationship_graph

        style = personality_evolution.get_style_profile() or {}
        graph = relationship_graph.get_graph()

        return jsonify({
            "communication_style": style,
            "relationship_count": len(graph),
            "people_tracked": len(relationship_graph.get_people()),
            "places_tracked": len(relationship_graph.get_places())
        })
    except Exception:
        LOGGER.exception("Evolution data failed")
        return jsonify({})


@bp.route("/api/personality/templates", methods=["GET"])
def get_personality_templates():
    """Get available personality templates."""
    try:
        from core.templates import personality_templates

        return jsonify(personality_templates.get_templates())
    except Exception:
        LOGGER.exception("Templates error")
        return api_error("internal_error", 500)


@bp.route("/api/personality/templates/<template_name>", methods=["GET"])
def get_specific_personality_template(template_name):
    """Get a specific personality template."""
    try:
        from core.templates import personality_templates

        template = personality_templates.get_template(template_name)
        return jsonify(template)
    except Exception:
        LOGGER.exception("Template error")
        return api_error("not_found", 404)


@bp.route("/api/personality/custom", methods=["POST"])
def save_custom_personality():
    """Save a custom personality template."""
    try:
        from core.templates import personality_templates

        data = request.json or {}
        name = data.get("name")
        traits = data.get("traits", [])
        tone = data.get("tone", "warm and conversational")
        formality = data.get("formality", "casual — like talking to a close friend")
        humor = data.get("humor", "dry wit, occasional playful teasing")
        emojis = data.get("emojis", True)
        interests = data.get("interests", [])

        if not name:
            return jsonify({"error": "Name is required"}), 400

        personality_templates.save_custom_personality(
            name=name, traits=traits, tone=tone, formality=formality,
            humor=humor, emojis=emojis, interests=interests
        )

        return jsonify({"status": "ok", "name": name})
    except Exception:
        LOGGER.exception("Custom personality error")
        return api_error("internal_error", 500)


@bp.route("/api/personality/custom/templates", methods=["GET"])
def get_custom_personality_templates():
    """Get all custom personality templates."""
    try:
        from core.templates import personality_templates

        return jsonify(personality_templates.get_custom_personality_templates())
    except Exception:
        LOGGER.exception("Custom templates error")
        return api_error("internal_error", 500)


@bp.route("/api/personality/mood-adjustments", methods=["POST"])
def get_mood_adjustments():
    """Get personality adjustments based on detected mood."""
    try:
        from core.templates import mood_dependent_personality

        data = request.json or {}
        base_personality = data.get("base_personality")
        emotion_data = data.get("emotion_data")

        adjusted_personality = mood_dependent_personality.get_mood_adjustments(
            base_personality, emotion_data
        )

        return jsonify({"adjusted_personality": adjusted_personality})
    except Exception:
        LOGGER.exception("Mood adjustments error")
        return api_error("internal_error", 500)
