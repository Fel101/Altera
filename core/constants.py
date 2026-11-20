"""
Altera - Shared constants for the application.

Centralizes magic numbers, limits, and configuration values to avoid
scattered hardcoded values throughout the codebase.
"""

# ============================================================
# Database Constants
# ============================================================
from core.paths import data_path
# Re-export the inert shared limit used by Flask and the private gateway.
from core.http_limits import MAX_UPLOAD_BYTES as MAX_UPLOAD_BYTES

DB_PATH = str(data_path('conversations.db'))
FTS_TABLE = "messages_fts"

# Default query limits
DEFAULT_LIMIT = 50
MAX_SEARCH_RESULTS = 100
MOOD_HISTORY_LIMIT = 30
RECENT_MESSAGES_LIMIT = 5
LIFE_EVENTS_LIMIT = 50

# Offline queue limits
OFFLINE_QUEUE_MAX_SIZE = 50

# ============================================================
# LLM Configuration Defaults
# ============================================================
LLM_TEMPERATURE_DEFAULT = 0.85
LLM_TEMPERATURE_ANALYSIS = 0.1  # Low temp for consistent analysis
LLM_MAX_TOKENS_DEFAULT = 200
LLM_MAX_TOKENS_LONG = 300

# ============================================================
# Time-based Constants
# ============================================================
PROACTIVE_CHECKIN_HOURS = 4
MOOD_TREND_DAYS = 7
FADING_RELATIONSHIP_THRESHOLD_DAYS = 30
NEW_RELATIONSHIP_WINDOW_DAYS = 14

# ============================================================
# Input Validation Limits
# ============================================================
MAX_MESSAGE_LENGTH = 10000
MIN_MESSAGE_LENGTH = 1
MAX_LLM_PROMPT_LENGTH = 8000

# Most memories /api/memory/dedup will compare in one (synchronous) request. Comparison is quadratic; 2000 is well
# under a second, and the response says when it examined only the newest.
MAX_DEDUP_MEMORIES = 2000
# Longest text the TTS endpoint will speak in one request.
MAX_TTS_CHARS = 5000

# ============================================================
# Depth Scoring Constants (Relationships)
# ============================================================
DEPTH_SCORE_MAX = 10.0
DEPTH_LEVELS = {
    "core": 8.0,
    "very_close": 6.0,
    "close": 4.0,
    "casual": 2.0,
    "acquaintance": 0.0,
}

# Recency bonuses for depth scoring
RECENCY_BONUSES = {
    "very_recent": (7, 2.0),      # < 7 days
    "recent": (30, 1.5),          # < 30 days
    "somewhat_recent": (90, 0.8), # < 90 days
    "distant": (float('inf'), 0.2) # > 90 days
}

# ============================================================
# Confidence Score Constants
# ============================================================
BASE_CONFIDENCE_SCORES = {
    "birthday": 0.85,
    "achievement": 0.75,
    "trip": 0.7,
    "work": 0.8,
    "relationship": 0.75,
    "health": 0.65,
    "holiday": 0.7,
    "general_milestone": 0.4,
}

MAX_CONFIDENCE = 0.98

# ============================================================
# Emotion Detection Constants
# ============================================================
VALID_EMOTIONS = {
    "happy", "sad", "angry", "anxious", "excited",
    "calm", "lonely", "grateful", "frustrated", "neutral"
}

INTENSITY_RANGE = (0.0, 1.0)
VALENCE_RANGE = (-1.0, 1.0)

# ============================================================
# Error Handling Patterns
# ============================================================
class AppError(Exception):
    """Base application error."""
    def __init__(self, message: str, code: str = "internal_error"):
        self.message = message
        self.code = code
        super().__init__(message)

class ValidationError(AppError):
    """Invalid input provided."""

class NotFoundError(AppError):
    """Resource not found."""

class ServiceUnavailableError(AppError):
    """External service unavailable (e.g., LLM)."""
    def __init__(self, message: str = "Service temporarily unavailable"):
        super().__init__(message, code="service_unavailable")

# ============================================================
# Logging Constants
# ============================================================
LOG_FORMAT = "[%(module)s] %(levelname)s: %(message)s"
LOG_LEVEL_DEFAULT = "INFO"

# ============================================================
# Sanitized Error Messages (never leak internals to clients)
# ============================================================
ERROR_MESSAGES = {
    "internal_error": "An internal error occurred. Please try again later.",
    "service_unavailable": "Service temporarily unavailable. Please try again later.",
    "validation_error": "Invalid input provided. Please check your request and try again.",
    "not_found": "The requested resource was not found.",
}

# ============================================================
# Helper: Sanitized error response (for use in API endpoints)
# ============================================================
def sanitized_error_response(code="internal_error", status_code=500):
    """Return a sanitized (payload, status_code) pair that never leaks internal details.

    The full exception is logged server-side; the client only gets a generic message.
    Callers should log the real error themselves before calling this function.

    Note this returns a TUPLE. A route that writes `jsonify(sanitized_error_response(...))` serialises the tuple
    itself — the client gets HTTP 200 and a JSON array, `[{...}, 500]`, so `response.error` is undefined and the
    failure reads as a success. Routes should use api_error() below instead, which cannot be misused that way.
    """
    return {
        "error": ERROR_MESSAGES.get(code, ERROR_MESSAGES["internal_error"]),
        "code": code,
    }, status_code


def api_error(code="internal_error", status_code=500):
    """A sanitized error as a ready-to-return Flask response: `return api_error("not_found", 404)`."""
    from flask import jsonify

    payload, status = sanitized_error_response(code, status_code)
    return jsonify(payload), status
