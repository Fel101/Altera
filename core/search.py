"""
Altera — Conversation Search (Phase 8)

Full-text search across all conversations with filters for date range, mood, topic keywords.
Uses SQLite FTS5 for fast searching without LLM dependency.
"""

from core.constants import FTS_TABLE
from core.datastore import get_datastore

DEFAULT_LIMIT = 50


class ConversationSearch:
    """Full-text search engine for Altera's conversation data."""

    def __init__(self, db_path=None):
        self.db_path = db_path

    def _ensure_fts(self):
        """Ensure FTS5 virtual table exists and is synced with messages."""
        store = get_datastore()
        store.ensure_fts_table(FTS_TABLE)

    def search(self, query, session_id=None, mood=None, date_from=None, date_to=None, role=None, limit=DEFAULT_LIMIT):
        """Search conversations with optional filters.

        Args:
            query: Search text (FTS5 full-text search)
            session_id: Filter by specific session
            mood: Filter by message mood ('happy', 'sad', 'stressed', etc.)
            date_from: Start date filter (ISO format string or datetime)
            date_to: End date filter (ISO format string or datetime)
            role: Filter by role ('user' or 'assistant')
            limit: Maximum results to return

        Returns:
            List of result dicts with message content and metadata
        """
        store = get_datastore()
        self._ensure_fts()
        return store.fts_search(
            query, fts_table_name=FTS_TABLE,
            session_id=session_id, mood=mood,
            date_from=date_from, date_to=date_to, role=role, limit=limit
        )

    def search_by_mood(self, mood, limit=20):
        """Search messages by mood type."""
        store = get_datastore()
        return store.search_by_mood(mood, limit)

    def search_by_session(self, session_id, query=None, limit=100):
        """Search within a specific session."""
        store = get_datastore()
        return store.search_by_session(session_id, query=query, limit=limit)

    def get_search_stats(self):
        """Get statistics about searchable content."""
        store = get_datastore()
        self._ensure_fts()
        return store.get_search_stats(FTS_TABLE)


# Singleton instance
conversation_search = ConversationSearch()
