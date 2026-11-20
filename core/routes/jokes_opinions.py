"""Jokes, opinions, and humor routes extracted from admin.py (Session 6: Blueprints).

Routes for inside joke tracking, opinion extraction, and humor style analysis.
"""

import logging
import re
from flask import Blueprint, request, jsonify

from core.constants import MAX_MESSAGE_LENGTH, api_error

LOGGER = logging.getLogger(__name__)

bp = Blueprint('jokes_opinions', __name__)

# See core/routes/core_api.py: a leading '-' in a character class is a literal hyphen, not a range.
_CONTROL_CHARS = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')


# ============================================================
# Routes — Jokes & Humor (Phase 10)
# ============================================================

@bp.route("/api/jokes/callbacks", methods=["GET"])
def get_joke_callbacks():
    """Get inside joke callbacks and shared references."""
    try:
        from core.jokes import joke_tracker

        current_topic = request.args.get("topic")
        limit = int(request.args.get("limit", 3))

        callbacks = joke_tracker.get_callback_material(current_topic=current_topic, limit=limit)
        return jsonify({"callbacks": callbacks})
    except Exception:
        LOGGER.exception("Joke callbacks error")
        return api_error("internal_error", 500)


@bp.route("/api/jokes/extract", methods=["POST"])
def extract_jokes_and_references():
    """Extract jokes and references from a message."""
    try:
        from core.jokes import joke_tracker

        data = request.json or {}
        text_raw = data.get("text", "")

        # Validate input
        if text_raw is None:
            return jsonify({"error": "Text is required"}), 400
        stripped = text_raw.strip()
        if not stripped:
            return jsonify({"error": "Text cannot be empty"}), 400

        if len(stripped) > MAX_MESSAGE_LENGTH:
            return jsonify({"error": f"Text too long (max {MAX_MESSAGE_LENGTH} characters)"}), 413
        text = _CONTROL_CHARS.sub('', stripped)

        session_id = data.get("session_id")

        result = joke_tracker.extract_jokes_and_references(text, session_id)
        return jsonify(result)
    except Exception:
        LOGGER.exception("Joke extraction error")
        return api_error("internal_error", 500)


@bp.route("/api/opinions/user", methods=["GET"])
def get_user_opinions():
    """Get stored user opinions."""
    try:
        from core.opinions import opinion_tracker

        opinions = opinion_tracker.get_user_opinions()
        return jsonify({"opinions": opinions})
    except Exception:
        LOGGER.exception("User opinions error")
        return api_error("internal_error", 500)


@bp.route("/api/opinions/extract", methods=["POST"])
def extract_opinions():
    """Extract opinions from a message."""
    try:
        from core.opinions import opinion_tracker

        data = request.json or {}
        text_raw = data.get("text", "")

        # Validate input
        if text_raw is None:
            return jsonify({"error": "Text is required"}), 400
        stripped = text_raw.strip()
        if not stripped:
            return jsonify({"error": "Text cannot be empty"}), 400

        if len(stripped) > MAX_MESSAGE_LENGTH:
            return jsonify({"error": f"Text too long (max {MAX_MESSAGE_LENGTH} characters)"}), 413
        text = _CONTROL_CHARS.sub('', stripped)

        session_id = data.get("session_id")

        results = opinion_tracker.analyze_opinions(text, session_id)
        return jsonify({"topics_with_opinions": results})
    except Exception:
        LOGGER.exception("Opinion extraction error")
        return api_error("internal_error", 500)


@bp.route("/api/humor/style", methods=["GET"])
def get_humor_style():
    """Get the user's humor style."""
    try:
        from core.opinions import humor_matcher

        style = humor_matcher.get_humor_style()
        return jsonify({"humor_style": style})
    except Exception:
        LOGGER.exception("Humor style error")
        return api_error("internal_error", 500)


@bp.route("/api/humor/analyze", methods=["POST"])
def analyze_humor_style():
    """Analyze the user's humor style from messages."""
    try:
        from core.opinions import humor_matcher

        data = request.json or {}
        text_samples = data.get("text_samples")
        session_id = data.get("session_id")

        result = humor_matcher.analyze_humor_style(text_samples=text_samples, session_id=session_id)
        return jsonify({"humor_analysis": result})
    except Exception:
        LOGGER.exception("Humor analysis error")
        return api_error("internal_error", 500)
