"""
Altera — DataStore (Session 4 of Architecture Refactor)

Owns all SQLite data access for the user_profile key-value store and relational tables.
Core modules call these typed accessors instead of raw SQL queries.

This is the deep interface for "how Altera's data persists."
"""

import json
import os
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime


# ============================================================
# Data Classes (typed representations of stored data)
# ============================================================

@dataclass
class UserProfile:
    """Represents a row from the user_profile key-value store."""
    key: str
    value: dict  # Parsed JSON value
    updated_at: str = ""


@dataclass
class SessionInfo:
    """Represents a conversation session."""
    id: int
    name: str
    tags: str = ""
    archived: bool = False
    created_at: str = ""
    updated_at: str = ""
    avatar_id: str = ""              # the companion this chat belongs to ('' for a chat from before companions)


@dataclass
class MessageRecord:
    """Represents a chat message."""
    id: int
    session_id: int
    role: str  # 'user', 'assistant', or 'system'
    content: str
    mood: dict | None = None
    timestamp: str = ""


@dataclass
class LifeEvent:
    """Represents a life event extracted from conversation."""
    id: int
    session_id: int
    event_type: str  # birthday, achievement, trip, work, relationship, health, holiday, general_milestone
    detail: str
    confidence: float = 0.7
    timestamp: str = ""
    raw_text: str = ""


# ============================================================
# Known user_profile keys (typed constants for type safety)
# ============================================================

class ProfileKeys:
    """Typed constants for all known user_profile key values."""
    RELATIONSHIP_GRAPH = "relationship_graph"
    COMMUNICATION_STYLE = "communication_style"
    USER_OPINIONS = "user_opinions"
    HUMOR_STYLE = "humor_style"
    JOKES_AND_REFERENCES = "jokes_and_references"
    FOLLOW_UPS = "follow_ups"
    CUSTOM_PERSONALITY_TEMPLATES = "custom_personality_templates"
    ONBOARDING_COMPLETE = "onboarding_complete"
    LAST_ACTIVE_SESSION = "last_active_session"


# ============================================================
# DataStore (the main deep interface — owns all SQLite access)
# ============================================================

class DataStore:
    """Owns all SQLite data persistence for Altera.

    Modules depend on interfaces, not table schemas. Migration is contained here.
    Mockable for testing.

    Usage:
        store = DataStore("data/conversations.db")

        # user_profile key-value access
        graph = store.get_relationship_graph()
        store.set_communication_style({"formality": "casual", "tone": "warm"})

        # session management
        session_id = store.create_session("New Chat")
        sessions = store.list_sessions(archived=False)

        # message access
        store.save_message(session_id, "user", "Hello!")
        messages = store.get_messages(session_id, limit=15)
    """

    def __init__(self, db_path=None):
        from core.paths import data_path
        self.db_path = str(db_path if db_path is not None else data_path('conversations.db'))
        self._schema_for = None              # absolute path of the database file whose schema is known to exist
        self._schema_lock = threading.Lock()

    def _ensure_schema(self) -> None:
        """Create the tables the first time this store opens a database file that does not have them yet.

        A fresh checkout has no data/ folder, and core/search.py opens the store while it is being imported (before altera.py's
        __main__ block calls init_db), so the first query used to fail with "no such table: messages". init_db() selects WAL
        and applies additive, idempotent schema migrations while preserving existing records.
        The check is keyed on the absolute path because db_path is relative: a changed working directory is a different file.
        """
        target = os.path.abspath(self.db_path)
        if self._schema_for == target:
            return
        with self._schema_lock:
            if self._schema_for != target:
                init_db(self.db_path)
                self._schema_for = target

    # ----------------------------------------------------------
    # Connection helper (centralized DB connection creation)
    # ----------------------------------------------------------

    def _connect(self):
        """Create a new database connection with standard settings."""
        self._ensure_schema()
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # ----------------------------------------------------------
    # user_profile Key-Value Store (generic accessors)
    # ----------------------------------------------------------

    def get_key(self, key: str) -> dict | None:
        """Get a parsed JSON value from the user_profile table.

        Args:
            key: The profile key to look up.

        Returns:
            Parsed dict value, or None if not found/invalid.
        """
        conn = self._connect()
        try:
            cursor = conn.execute(
                "SELECT value FROM user_profile WHERE key = ?", (key,)
            )
            row = cursor.fetchone()

            if row and row["value"]:
                return json.loads(row["value"])
            return None
        except (json.JSONDecodeError, TypeError):
            return None
        finally:
            conn.close()

    def dataset_epoch(self):
        """Stable across restarts, changed on restore so stale browser drafts cannot be replayed."""
        conn = self._connect()
        try:
            with conn:
                candidate = json.dumps({"value": uuid.uuid4().hex, "required": False})
                conn.execute("INSERT OR IGNORE INTO user_profile(key,value) VALUES('dataset_epoch',?)", (candidate,))
                return json.loads(conn.execute("SELECT value FROM user_profile WHERE key='dataset_epoch'").fetchone()[0])
        finally:
            conn.close()

    def set_key(self, key: str, value: dict) -> None:
        """Set a JSON-serializable value in the user_profile table.

        Args:
            key: The profile key to set.
            value: Dict value (will be JSON-encoded).
        """
        conn = self._connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO user_profile (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
                (key, json.dumps(value))
            )
            conn.commit()
        finally:
            conn.close()

    def delete_key(self, key: str) -> None:
        """Delete a key from the user_profile table."""
        conn = self._connect()
        try:
            conn.execute("DELETE FROM user_profile WHERE key = ?", (key,))
            conn.commit()
        finally:
            conn.close()

    def get_all_keys(self) -> list[UserProfile]:
        """Get all profile keys as UserProfile objects."""
        conn = self._connect()
        try:
            cursor = conn.execute("SELECT key, value, updated_at FROM user_profile")
            profiles = []
            for row in cursor.fetchall():
                try:
                    val = json.loads(row["value"]) if row["value"] else {}
                    profiles.append(UserProfile(
                        key=row["key"],
                        value=val,
                        updated_at=row["updated_at"] or ""
                    ))
                except (json.JSONDecodeError, TypeError):
                    profiles.append(UserProfile(key=row["key"], value={}, updated_at=row["updated_at"] or ""))
            return profiles
        finally:
            conn.close()

    def get_user_profile(self) -> dict:
        """Get all user profile data as a combined dict.

        Returns:
            Combined dict of all known profile keys and their values.
        """
        result = {}
        for key in ProfileKeys.__dict__:
            if not key.startswith("_"):
                val = getattr(ProfileKeys, key)
                data = self.get_key(val)
                if data is not None:
                    result[val] = data
        return result

    def set_user_profile(self, data: dict) -> None:
        """Set user profile data. Accepts a dict of {key: value} pairs.

        Args:
            data: Dict mapping profile keys to their values.
        """
        for key, value in data.items():
            self.set_key(key, value)

    # ----------------------------------------------------------
    # user_profile Key-Value Store (typed accessors)
    # ----------------------------------------------------------

    def get_relationship_graph(self) -> dict:
        """Get the relationship graph data."""
        return self.get_key(ProfileKeys.RELATIONSHIP_GRAPH) or {}

    def set_relationship_graph(self, graph: dict) -> None:
        """Set the relationship graph data."""
        self.set_key(ProfileKeys.RELATIONSHIP_GRAPH, graph)

    def get_communication_style(self) -> dict | None:
        """Get the user's communication style profile."""
        return self.get_key(ProfileKeys.COMMUNICATION_STYLE)

    def set_communication_style(self, style: dict) -> None:
        """Set/merge the user's communication style profile.

        Args:
            style: New style data to merge with existing (keys in style override).
        """
        existing = self.get_communication_style() or {}
        merged = {**existing, **style}
        if "first_analyzed" not in merged:
            merged["first_analyzed"] = datetime.now().isoformat()
        merged["last_updated"] = datetime.now().isoformat()
        self.set_key(ProfileKeys.COMMUNICATION_STYLE, merged)

    def get_user_opinions(self) -> dict:
        """Get the user's tracked opinions."""
        return self.get_key(ProfileKeys.USER_OPINIONS) or {}

    def set_user_opinions(self, opinions: dict) -> None:
        """Set the user's tracked opinions (full replacement)."""
        self.set_key(ProfileKeys.USER_OPINIONS, opinions)

    def get_humor_style(self) -> dict | None:
        """Get the detected humor style."""
        return self.get_key(ProfileKeys.HUMOR_STYLE)

    def set_humor_style(self, style: dict) -> None:
        """Set the detected humor style."""
        self.set_key(ProfileKeys.HUMOR_STYLE, style)

    def get_jokes_and_references(self) -> dict:
        """Get inside jokes and shared references data."""
        return self.get_key(ProfileKeys.JOKES_AND_REFERENCES) or {}

    def set_jokes_and_references(self, data: dict) -> None:
        """Set inside jokes and shared references data (full replacement)."""
        self.set_key(ProfileKeys.JOKES_AND_REFERENCES, data)

    def get_follow_ups(self) -> list[dict]:
        """Get all follow-up items."""
        return self.get_key(ProfileKeys.FOLLOW_UPS) or []

    def set_follow_ups(self, items: list[dict]) -> None:
        """Set all follow-up items (full replacement)."""
        self.set_key(ProfileKeys.FOLLOW_UPS, items)

    def get_custom_personality_templates(self) -> dict:
        """Get custom personality templates."""
        return self.get_key(ProfileKeys.CUSTOM_PERSONALITY_TEMPLATES) or {}

    def set_custom_personality_templates(self, templates: dict) -> None:
        """Set custom personality templates (full replacement)."""
        self.set_key(ProfileKeys.CUSTOM_PERSONALITY_TEMPLATES, templates)

    def get_last_active_session(self) -> int | None:
        """Get the last active session ID from user profile."""
        data = self.get_key(ProfileKeys.LAST_ACTIVE_SESSION)
        if data and isinstance(data, dict):
            return data.get("session_id")
        return None

    def set_last_active_session(self, session_id: int) -> None:
        """Set the last active session ID in user profile."""
        self.set_key(ProfileKeys.LAST_ACTIVE_SESSION, {"session_id": session_id})

    # ----------------------------------------------------------
    # Session Management
    # ----------------------------------------------------------

    def create_session(self, name: str = "New Conversation") -> int:
        """Create a new conversation session.

        Args:
            name: Human-readable session name (first 30 chars enforced).

        Returns:
            The new session's ID.
        """
        conn = self._connect()
        try:
            cursor = conn.execute(
                "INSERT INTO sessions (name, created_at, updated_at) VALUES (?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (name[:30],)
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()

    def get_session(self, session_id: int) -> SessionInfo | None:
        """Get a single session by ID.

        Args:
            session_id: The session ID to look up.

        Returns:
            SessionInfo object, or None if not found.
        """
        conn = self._connect()
        try:
            cursor = conn.execute(
                "SELECT id, name, tags, archived, created_at, updated_at, avatar_id FROM sessions WHERE id = ?",
                (session_id,)
            )
            row = cursor.fetchone()

            if row:
                return self._session_info(row)
            return None
        finally:
            conn.close()

    def update_session(self, session_id: int, **kwargs) -> None:
        """Update a session's fields.

        Args:
            session_id: The session to update.
            **kwargs: Fields to update (name, tags, archived).
        """
        conn = self._connect()
        try:
            updates = []
            params = []

            if "name" in kwargs:
                updates.append("name = ?")
                params.append(kwargs["name"][:30])
            if "tags" in kwargs:
                updates.append("tags = ?")
                params.append(kwargs["tags"])
            if "archived" in kwargs:
                updates.append("archived = ?")
                params.append(bool(kwargs["archived"]))

            if updates:
                updates.append("updated_at = CURRENT_TIMESTAMP")
                sql = f"UPDATE sessions SET {', '.join(updates)} WHERE id = ?"
                params.append(session_id)
                conn.execute(sql, params)
                conn.commit()
        finally:
            conn.close()

    def list_sessions(self, archived: bool = False) -> list[SessionInfo]:
        """List sessions ordered by last activity.

        Args:
            archived: If True, return only archived sessions. If False, only active. None for all.

        Returns:
            List of SessionInfo objects sorted by updated_at DESC.
        """
        conn = self._connect()
        try:
            if archived is None:
                query = "SELECT id, name, tags, archived, created_at, updated_at, avatar_id FROM sessions ORDER BY updated_at DESC"
            elif archived:
                query = "SELECT id, name, tags, archived, created_at, updated_at, avatar_id FROM sessions WHERE archived=1 ORDER BY updated_at DESC"
            else:
                query = "SELECT id, name, tags, archived, created_at, updated_at, avatar_id FROM sessions WHERE archived=0 ORDER BY updated_at DESC"

            cursor = conn.execute(query)
            return [self._session_info(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    def archive_session(self, session_id: int) -> None:
        """Archive a session (hide from default view)."""
        self.update_session(session_id, archived=True)

    def unarchive_session(self, session_id: int) -> None:
        """Un-archive a session (show in default view)."""
        self.update_session(session_id, archived=False)

    def rename_session(self, session_id: int, name: str) -> None:
        """Rename a session."""
        self.update_session(session_id, name=name)

    def delete_session(self, session_id: int, *, cleanup_only=False) -> None:
        """Delete a session and all its messages (cascade)."""
        conn = self._connect()
        try:
            if conn.execute("SELECT 1 FROM chat_turns WHERE session_id=? AND status IN ('sending','streaming')", (session_id,)).fetchone():
                raise ValueError("Wait for the reply to finish before deleting this conversation.")
            from core.voice_notes import get_voice_note_cleanup_service, get_voice_note_service
            notes = get_voice_note_cleanup_service(self) if cleanup_only else get_voice_note_service(self)
            notes.clear_session(session_id, allow_interrupted=cleanup_only)
            from core.presence import get_presence_service
            get_presence_service(self).clear_session(session_id)
            # The caller may already own maintenance (companion removal). Keep
            # derived cleanup in this transaction without lazy service recovery.
            from core.reflection import ReflectionService
            ReflectionService._clear(conn, 'session_id', session_id)
            from core.episodes import delete_session as delete_episodes
            delete_episodes(conn, session_id)
            conn.execute('DELETE FROM episode_recall_turns WHERE user_message_id IN (SELECT id FROM messages WHERE session_id=?) '
                         'OR assistant_message_id IN (SELECT id FROM messages WHERE session_id=?)', (session_id, session_id))
            conn.execute('DELETE FROM voice_notes WHERE session_id=?', (session_id,))
            conn.execute("DELETE FROM message_version_current WHERE message_id IN (SELECT id FROM messages WHERE session_id=?)", (session_id,))
            conn.execute("DELETE FROM message_versions WHERE message_id IN (SELECT id FROM messages WHERE session_id=?)", (session_id,))
            conn.execute("INSERT OR IGNORE INTO chat_request_tombstones(request_id) SELECT request_id FROM chat_turns WHERE session_id=?", (session_id,))
            conn.execute("DELETE FROM chat_turn_events WHERE request_id IN (SELECT request_id FROM chat_turns WHERE session_id=?)", (session_id,))
            conn.execute("DELETE FROM chat_turns WHERE session_id=?", (session_id,))
            self._invalidate_session_knowledge(conn, session_id)
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            conn.commit()
        finally:
            conn.close()

    def get_session_name(self, session_id: int) -> str | None:
        """Get just the name of a session (for split operations)."""
        conn = self._connect()
        try:
            cursor = conn.execute(
                "SELECT name FROM sessions WHERE id = ?", (session_id,)
            )
            row = cursor.fetchone()
            return row["name"] if row else None
        finally:
            conn.close()

    def get_last_session_updated_at(self) -> str | None:
        """Get the most recent session's updated_at timestamp."""
        conn = self._connect()
        try:
            cursor = conn.execute(
                "SELECT updated_at FROM sessions ORDER BY updated_at DESC LIMIT 1"
            )
            row = cursor.fetchone()
            return row["updated_at"] if row else None
        finally:
            conn.close()

    @staticmethod
    def _session_info(row) -> SessionInfo:
        return SessionInfo(
            id=row["id"],
            name=row["name"],
            tags=row["tags"] or "",
            archived=bool(row["archived"]),
            created_at=row["created_at"] or "",
            updated_at=row["updated_at"] or "",
            avatar_id=row["avatar_id"] or "",
        )

    # ----------------------------------------------------------
    # Companions: one avatar, one continuous chat
    # ----------------------------------------------------------

    def set_session_avatar(self, session_id: int, avatar_id: str) -> None:
        """Make a session the chat of an avatar (a companion)."""
        conn = self._connect()
        try:
            conn.execute("UPDATE sessions SET avatar_id = ? WHERE id = ?", (avatar_id or "", session_id))
            conn.commit()
        finally:
            conn.close()

    def get_avatar_session_id(self, avatar_id: str) -> int | None:
        """The chat of an avatar: its newest session that is not archived, or None if it has none yet."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT id FROM sessions WHERE avatar_id = ? AND archived = 0 ORDER BY updated_at DESC, id DESC LIMIT 1",
                (avatar_id,)).fetchone()
            return row["id"] if row else None
        finally:
            conn.close()

    def ensure_avatar_session(self, avatar_id: str, name: str = "") -> int:
        """The avatar's chat, created if it does not exist yet."""
        found = self.get_avatar_session_id(avatar_id)
        if found:
            return found
        session_id = self.create_session(name or avatar_id)
        self.set_session_avatar(session_id, avatar_id)
        return session_id

    def list_avatar_sessions(self, avatar_id: str) -> list[SessionInfo]:
        """Every session that belongs to an avatar (archived ones too), newest first."""
        return [s for s in self.list_sessions(archived=None) if s.avatar_id == avatar_id]

    @staticmethod
    def _retire_session_requests(conn, session_ids):
        marks = ','.join('?' for _ in session_ids)
        if conn.execute(f"SELECT 1 FROM chat_turns WHERE session_id IN ({marks}) AND status IN ('sending','streaming')", session_ids).fetchone():
            raise ValueError('Wait for the reply to finish before changing this conversation.')
        conn.execute(f'INSERT OR IGNORE INTO chat_request_tombstones(request_id) SELECT request_id FROM chat_turns WHERE session_id IN ({marks})', session_ids)
        conn.execute(f'DELETE FROM chat_turn_events WHERE request_id IN (SELECT request_id FROM chat_turns WHERE session_id IN ({marks}))', session_ids)
        conn.execute(f'DELETE FROM chat_turns WHERE session_id IN ({marks})', session_ids)

    def move_messages(self, from_session_ids: list, to_session_id: int, retire_requests=False) -> int:
        """Move every message of the given sessions into another one (ids keep their order). Returns how many moved."""
        ids = [int(i) for i in from_session_ids if int(i) != int(to_session_id)]
        if not ids:
            return 0
        conn = self._connect()
        try:
            marks = ",".join("?" for _ in ids)
            if retire_requests:
                conn.execute('BEGIN IMMEDIATE')
                found = list(conn.execute(f'SELECT id,avatar_id FROM sessions WHERE id IN ({marks},?)', [*ids, to_session_id]))
                if {r[0] for r in found} != {*ids, to_session_id}:
                    raise ValueError('A conversation changed. Reload before merging.')
                owners = {row[1] or '' for row in found}
                if len(owners) != 1:
                    raise ValueError('Conversations belonging to different companions cannot be merged.')
                self._retire_session_requests(conn, [*ids, to_session_id])
            cursor = conn.execute(f"UPDATE messages SET session_id = ? WHERE session_id IN ({marks})", [to_session_id, *ids])
            conn.execute("UPDATE sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (to_session_id,))
            if retire_requests:
                for session_id in [*ids, to_session_id]:
                    self._invalidate_session_knowledge(conn, session_id)
            conn.commit()
            return cursor.rowcount
        finally:
            conn.close()

    def count_user_messages(self, session_id: int, after_id: int = 0) -> int:
        """How many messages the person has sent in a session (after a given message id)."""
        conn = self._connect()
        try:
            return conn.execute(
                "SELECT COUNT(*) AS cnt FROM messages WHERE session_id = ? AND role = 'user' AND id > ?",
                (session_id, after_id)).fetchone()["cnt"]
        finally:
            conn.close()

    def mark_knowledge_dirty(self, session_id):
        conn = self._connect()
        try:
            with conn:
                self._invalidate_session_knowledge(conn, session_id)
        finally:
            conn.close()

    def split_session_at(self, session_id, message_id):
        """Split atomically, retaining owner, source IDs and timestamps; retire old replay copies."""
        conn = self._connect()
        try:
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                source = conn.execute('SELECT name,avatar_id FROM sessions WHERE id=?', (session_id,)).fetchone()
                if not source or not conn.execute('SELECT 1 FROM messages WHERE session_id=? AND id>?', (session_id, message_id)).fetchone():
                    raise ValueError('No messages found after the split point.')
                if conn.execute("SELECT 1 FROM chat_turns WHERE session_id=? AND status IN ('sending','streaming')", (session_id,)).fetchone():
                    raise ValueError('Wait for the reply to finish before splitting.')
                new_id = conn.execute('INSERT INTO sessions(name,avatar_id) VALUES(?,?)', ('Split from ' + (source['name'] or 'Conversation'), source['avatar_id'] or '')).lastrowid
                self._retire_session_requests(conn, [session_id])
                conn.execute('UPDATE messages SET session_id=? WHERE session_id=? AND id>?', (new_id, session_id, message_id))
                conn.execute('UPDATE sessions SET updated_at=CURRENT_TIMESTAMP WHERE id=?', (session_id,))
                self._invalidate_session_knowledge(conn, session_id)
                self._invalidate_session_knowledge(conn, new_id)
                return new_id
        finally:
            conn.close()

    def last_message_at(self, session_id: int) -> str:
        """Timestamp of the newest message in a session ('' when it has none)."""
        conn = self._connect()
        try:
            row = conn.execute("SELECT timestamp FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT 1",
                               (session_id,)).fetchone()
            return (row["timestamp"] or "") if row else ""
        finally:
            conn.close()

    def get_all_sessions_for_export(self) -> list[dict]:
        """Get all sessions for export (admin endpoint support)."""
        conn = self._connect()
        try:
            cursor = conn.execute("SELECT * FROM sessions ORDER BY created_at DESC")
            return [dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    # ----------------------------------------------------------
    # Message Management
    # ----------------------------------------------------------

    def get_message(self, message_id):
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
            if row:
                return MessageRecord(id=row["id"], session_id=row["session_id"], role=row["role"],
                                     content=row["content"], mood=json.loads(row["mood"]) if row["mood"] else None,
                                     timestamp=row["timestamp"] or "")
            return None
        finally:
            conn.close()

    def get_messages_before(self, session_id, cutoff, limit=40):
        # Read before LIMIT so historical regeneration cannot include later messages.
        conn = self._connect()
        try:
            rows = conn.execute("SELECT * FROM messages WHERE session_id=? AND id<? ORDER BY id DESC LIMIT ?",
                                (session_id, cutoff, limit)).fetchall()
            return [MessageRecord(id=r["id"], session_id=r["session_id"], role=r["role"], content=r["content"],
                                  timestamp=r["timestamp"] or "") for r in reversed(rows)]
        finally:
            conn.close()

    def replace_reply(self, message_id, session_id, content):
        """Replace an answer's text. Older callers used this; the durable turn path uses save_turn_message, which also
        keeps the answer it replaces so the person can go back to it."""
        conn = self._connect()
        try:
            with conn:
                version_id = self._archive_version(conn, message_id, session_id)
                changed = conn.execute("UPDATE messages SET content=?,mood=NULL WHERE id=? AND session_id=? AND role='assistant'",
                                       (content, message_id, session_id)).rowcount
                if not changed:
                    raise ValueError("The answer being replaced no longer exists.")
                self._point_version(conn, message_id, version_id, content)
                conn.execute("UPDATE sessions SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (session_id,))
                self._invalidate_session_knowledge(conn, session_id)
        finally:
            conn.close()

    # ---- earlier answers ------------------------------------------------------------------------------------------------
    # One invariant: `messages.content` always holds the text the person is looking at, and `message_versions` holds every
    # answer that message has had, one row each, whose `content` matches the row for the version showing. Version ids are
    # stable: the message's own id is its first answer (what memories, voice notes and the reply ledger point at), and each
    # regeneration or switch adds the next id. `message_version_current` is NULL until the first regeneration.
    @staticmethod
    def _archive_version(conn, message_id, session_id, content=None):
        """Write the text being replaced into its own version row, and return the version_id the new answer will take.

        The text on screen is kept under the id it already has (the message's own id the first time, or the current
        version's id after a switch); `content` is only needed by `show_message_version`, which passes the text being
        left when it differs from the row. Called inside the write transaction, before the row changes."""
        row = conn.execute("SELECT content FROM messages WHERE id=? AND session_id=?", (message_id, session_id)).fetchone()
        if row is None:
            raise ValueError('The answer being replaced no longer exists.')
        pointer = conn.execute("SELECT version_id FROM message_version_current WHERE message_id=?",
                               (message_id,)).fetchone()
        kept_id = pointer['version_id'] if pointer is not None else message_id
        text = row['content'] if content is None else content
        conn.execute("INSERT INTO message_versions(version_id,message_id,content) VALUES(?,?,?) "
                     "ON CONFLICT(message_id, version_id) DO UPDATE SET content=excluded.content",
                     (kept_id, message_id, text))
        highest = conn.execute("SELECT COALESCE(max(version_id), 0) FROM message_versions WHERE message_id=?",
                               (message_id,)).fetchone()[0]
        return max(highest, message_id) + 1

    @staticmethod
    def _point_version(conn, message_id, version_id, content):
        """Write `content` into version `version_id` and point the message at it. The message row is written with the same
        text immediately after, so the two never disagree about what is showing."""
        conn.execute("INSERT INTO message_versions(version_id,message_id,content) VALUES(?,?,?) "
                     "ON CONFLICT(message_id, version_id) DO UPDATE SET content=excluded.content",
                     (version_id, message_id, content))
        if version_id == message_id:
            conn.execute("DELETE FROM message_version_current WHERE message_id=?", (message_id,))
        else:
            conn.execute("INSERT INTO message_version_current(message_id,version_id) VALUES(?,?) "
                         "ON CONFLICT(message_id) DO UPDATE SET version_id=excluded.version_id",
                         (message_id, version_id))

    def message_versions(self, message_id) -> list[dict]:
        """Every answer a message has had, oldest first, as [{version_id, content, current}]. `current` is True on
        exactly one entry.

        `message_versions` holds every answer, one row each, including the one showing; its `content` is the message
        row's text for that version. A message with no rows has never been regenerated: its own id and text are the one
        answer it has."""
        conn = self._connect()
        try:
            row = conn.execute("SELECT id, content FROM messages WHERE id=?", (message_id,)).fetchone()
            if row is None:
                return []
            # Copy the text out: a sqlite3.Row reads live values, so it would report the message's current text for every
            # version, not the text each version had.
            own_text = str(row["content"])
            pointer = conn.execute("SELECT version_id FROM message_version_current WHERE message_id=?",
                                   (message_id,)).fetchone()
            showing = pointer["version_id"] if pointer is not None else row["id"]
            saved = conn.execute("SELECT version_id, content FROM message_versions WHERE message_id=? ORDER BY version_id",
                                 (message_id,)).fetchall()
            if not saved:
                return [{"version_id": row["id"], "content": own_text, "current": True}]
            return [{"version_id": item["version_id"],
                     "content": own_text if item["version_id"] == showing else str(item["content"]),
                     "current": item["version_id"] == showing} for item in saved]
        finally:
            conn.close()

    def show_message_version(self, message_id, session_id, version_id, *, expected_content=None, dataset_epoch=None,
                             expected_owner=None):
        """Show an earlier answer again. The message row keeps its id (memories, voice notes and the ledger point at it),
        its text becomes the chosen version, and the text it held is kept as another version. Marked for knowledge repair,
        like a regenerated answer, because what the model and the notes read has changed."""
        from core.turns import get_turn_service
        with get_turn_service(self).lock:
            conn = self._connect()
            try:
                with conn:
                    conn.execute('BEGIN IMMEDIATE')
                    self._check_message_change(conn, message_id, session_id, expected_content, dataset_epoch, expected_owner)
                    if type(version_id) is not int:
                        raise ValueError("Choose one of this answer's earlier versions.")
                    # Read every text into plain strings before the first write: a sqlite3.Row reads live values, so a cell
                    # read after an UPDATE would report the new text rather than what was there.
                    row = conn.execute("SELECT content FROM messages WHERE id=? AND session_id=? AND role='assistant'",
                                       (message_id, session_id)).fetchone()
                    if row is None:
                        raise ValueError('That answer was not found.')
                    left = str(row["content"])
                    current = conn.execute("SELECT version_id FROM message_version_current WHERE message_id=?",
                                           (message_id,)).fetchone()
                    # With no pointer the message's own text is showing, so its own id is the current version.
                    showing = current["version_id"] if current is not None else message_id
                    if showing == version_id:
                        return {"message_id": message_id, "version_id": version_id, "content": left,
                                "changed": False, "knowledge_rebuild_pending": False}
                    if version_id == message_id:
                        # The message's own version: its text is the message row's text only while it is showing. Once the
                        # message has been regenerated the row holds the current text, so the row's own text is in
                        # `message_versions` under the message's id (written by `_archive_version`).
                        own = conn.execute("SELECT content FROM message_versions WHERE version_id=? AND message_id=?",
                                           (version_id, message_id)).fetchone()
                        chosen = str(own[0]) if own is not None else left
                    else:
                        kept = conn.execute("SELECT content FROM message_versions WHERE version_id=? AND message_id=?",
                                            (version_id, message_id)).fetchone()
                        if kept is None:
                            raise ValueError('That version was not found.')
                        chosen = str(kept[0])
                    # The message's own version always holds the message's own text, so record it before moving on.
                    conn.execute("INSERT INTO message_versions(version_id,message_id,content) VALUES(?,?,?) "
                                 "ON CONFLICT(message_id, version_id) DO UPDATE SET content=excluded.content",
                                 (message_id, message_id, left))
                    self._point_version(conn, message_id, version_id, chosen)
                    conn.execute("UPDATE messages SET content=?,mood=NULL WHERE id=? AND session_id=?",
                                 (chosen, message_id, session_id))
                    self._invalidate_session_knowledge(conn, session_id)
                    conn.execute("UPDATE sessions SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (session_id,))
                return {"message_id": message_id, "version_id": version_id, "content": chosen, "changed": True,
                        "knowledge_rebuild_pending": True}
            finally:
                conn.close()

    def save_turn_message(self, request_id, session_id, role, content, *, target_id=None, captured_memories=None):
        """Commit a durable turn's message and its ledger link together."""
        from core.turns import PROCESS_ID
        if role not in ('user', 'assistant') or (role == 'user' and target_id is not None):
            raise ValueError('Invalid turn message.')
        column = 'user_message_id' if role == 'user' else 'assistant_message_id'
        conn = self._connect()
        try:
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                turn = conn.execute('SELECT * FROM chat_turns WHERE request_id=?', (request_id,)).fetchone()
                if (not turn or turn['session_id'] != session_id or turn['process_id'] != PROCESS_ID
                        or turn['status'] not in ('sending', 'streaming') or turn[column] is not None
                        or (role == 'assistant' and turn['target_message_id'] != target_id)):
                    raise ValueError('The reply request is no longer available.')
                if target_id is None:
                    cursor = conn.execute('INSERT INTO messages (session_id,role,content) VALUES (?,?,?)',
                                          (session_id, role, content))
                    message_id = cursor.lastrowid
                    # An assistant message that has never been regenerated is its own version: no message_versions row
                    # exists yet, and the message's own id names the single version. A user message has no versions.
                else:
                    # Regenerating keeps the answer it replaces: the row keeps its id (memories, voice notes and the
                    # ledger point at it) and its old text is archived, so the person can go back to it.
                    version_id = self._archive_version(conn, target_id, session_id)
                    changed = conn.execute("UPDATE messages SET content=?,mood=NULL WHERE id=? AND session_id=? AND role='assistant'",
                                           (content, target_id, session_id)).rowcount
                    if not changed:
                        raise ValueError('The answer being replaced no longer exists.')
                    self._point_version(conn, target_id, version_id, content)
                    self._invalidate_session_knowledge(conn, session_id)
                    message_id = target_id
                conn.execute('UPDATE sessions SET updated_at=CURRENT_TIMESTAMP WHERE id=?', (session_id,))
                if role == 'assistant':
                    from core.expression import capture_pending
                    from core.memory_recovery import encode_intent
                    pending = capture_pending(conn, session_id, turn['user_message_id'], message_id)
                    memory_pending = encode_intent(captured_memories, pending)
                    linked = conn.execute('UPDATE chat_turns SET assistant_message_id=?,expression_pending=?,memory_pending=?,updated_at=CURRENT_TIMESTAMP WHERE request_id=?',
                                          (message_id, pending, memory_pending, request_id)).rowcount
                else:
                    linked = conn.execute(f'UPDATE chat_turns SET {column}=?,updated_at=CURRENT_TIMESTAMP WHERE request_id=?',
                                          (message_id, request_id)).rowcount
                if linked != 1:
                    raise ValueError('The reply request could not be linked.')
                return message_id
        finally:
            conn.close()

    @staticmethod
    def _invalidate_session_knowledge(conn, session_id):
        from core.expression import invalidate_session as invalidate_expression
        from core.presence import invalidate_session as invalidate_presence
        from core.episodes import invalidate_session as invalidate_episodes
        from core.reflection import invalidate_session as invalidate_reflection
        invalidate_expression(conn, session_id)
        invalidate_presence(conn, session_id, 'Conversation changed.')
        invalidate_episodes(conn, session_id, 'Conversation changed.')
        invalidate_reflection(conn, session_id, 'Conversation changed.')
        key = f"knowledge_dirty:{session_id}"
        old = conn.execute("SELECT value FROM user_profile WHERE key=?", (key,)).fetchone()
        revision = int((json.loads(old[0]) if old else {}).get("revision", 0)) + 1
        state = json.dumps({"pending": True, "revision": revision})
        conn.execute("INSERT INTO user_profile(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=CURRENT_TIMESTAMP",
                     (key, state))
        owner = conn.execute("SELECT COALESCE(avatar_id,'') FROM sessions WHERE id=?", (session_id,)).fetchone()
        if owner:
            conn.execute("UPDATE voice_notes SET status='stale',error_stage='context',error='Saved context changed. Record a new voice note.',updated_at=CURRENT_TIMESTAMP WHERE avatar_id=? AND error_stage!='deleted'", (owner[0],))
            conn.execute("INSERT INTO user_profile(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=CURRENT_TIMESTAMP",
                         ('knowledge_dirty_owner:' + (owner[0] or '@default'), json.dumps({'pending': True})))

    def change_message(self, message_id, session_id, content=None, delete=False, *, expected_content=None,
                       dataset_epoch=None, expected_owner=None):
        from core.turns import get_turn_service
        with get_turn_service(self).lock:
            if delete:
                conn = self._connect()
                try:
                    self._check_message_change(conn, message_id, session_id, expected_content, dataset_epoch, expected_owner)
                finally:
                    conn.close()
                from core.voice_notes import get_voice_note_service
                service = get_voice_note_service(self)
                service.clear_message(message_id)
            return self._change_message(message_id, session_id, content, delete, expected_content=expected_content,
                                        dataset_epoch=dataset_epoch, expected_owner=expected_owner)

    @staticmethod
    def _check_message_change(conn, message_id, session_id, expected_content, dataset_epoch, expected_owner):
        row = conn.execute("SELECT * FROM messages WHERE id=? AND session_id=?", (message_id, session_id)).fetchone()
        if not row:
            raise LookupError("Message not found in this conversation.")
        if expected_content is not None and row['content'] != expected_content:
            raise ValueError('This message changed. Reload and review it before changing it.')
        if expected_owner is not None:
            owner = conn.execute("SELECT COALESCE(avatar_id,'') FROM sessions WHERE id=?", (session_id,)).fetchone()
            if not owner or owner[0] != expected_owner:
                raise ValueError('This conversation changed companion. Reload before changing this message.')
        if dataset_epoch is not None:
            epoch = conn.execute("SELECT value FROM user_profile WHERE key='dataset_epoch'").fetchone()
            try:
                valid_epoch = bool(epoch and json.loads(epoch[0]).get('value') == dataset_epoch)
            except (ValueError, TypeError, AttributeError):
                valid_epoch = False
            if not valid_epoch:
                raise ValueError('Altera’s data changed. Reload and review this message before changing it.')
        if conn.execute("SELECT 1 FROM chat_turns WHERE session_id=? AND status IN ('sending','streaming')", (session_id,)).fetchone():
            raise ValueError('Stop the active reply and wait for it to finish before changing messages.')

    def _change_message(self, message_id, session_id, content=None, delete=False, *, expected_content=None,
                        dataset_epoch=None, expected_owner=None):
        """Change one message atomically; invalidate its replay copies and derived knowledge."""
        conn = self._connect()
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                self._check_message_change(conn, message_id, session_id, expected_content, dataset_epoch, expected_owner)
                related = "SELECT request_id FROM chat_turns WHERE user_message_id=? OR assistant_message_id=? OR target_message_id=?"
                conn.execute(f"INSERT OR IGNORE INTO chat_request_tombstones(request_id) {related}", (message_id,) * 3)
                conn.execute(f"DELETE FROM chat_turn_events WHERE request_id IN ({related})", (message_id,) * 3)
                conn.execute("DELETE FROM chat_turns WHERE user_message_id=? OR assistant_message_id=? OR target_message_id=?", (message_id,) * 3)
                if delete:
                    # An edited or deleted message is a correction: its earlier answers go with it, so none of them can be
                    # brought back afterwards.
                    conn.execute("DELETE FROM message_version_current WHERE message_id=?", (message_id,))
                    conn.execute("DELETE FROM message_versions WHERE message_id=?", (message_id,))
                    conn.execute("DELETE FROM messages WHERE id=?", (message_id,))
                else:
                    # A hand edit replaces the text outright; the earlier answers of a regenerated message are dropped, so
                    # the message's own text is its only version again. The row keeps its id.
                    conn.execute("DELETE FROM message_version_current WHERE message_id=?", (message_id,))
                    conn.execute("DELETE FROM message_versions WHERE message_id=?", (message_id,))
                    conn.execute("UPDATE messages SET content=?,mood=NULL WHERE id=?", (content, message_id))
                conn.execute("UPDATE sessions SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (session_id,))
                self._invalidate_session_knowledge(conn, session_id)
        finally:
            conn.close()


    def save_message(self, session_id: int, role: str, content: str, mood=None) -> int:
        """Save a chat message.

        Args:
            session_id: Parent session ID.
            role: 'user', 'assistant', or 'system'.
            content: Message text.
            mood: Optional emotion data dict (will be JSON-encoded).
        """
        conn = self._connect()
        try:
            cursor = conn.execute(
                "INSERT INTO messages (session_id, role, content, mood) VALUES (?, ?, ?, ?)",
                (session_id, role, content, json.dumps(mood) if mood else None)
            )
            conn.execute(
                "UPDATE sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (session_id,)
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()

    @staticmethod
    def _check_shared_photo_scope(conn, session_id, avatar_id, dataset_epoch):
        owner = conn.execute("SELECT COALESCE(avatar_id,'') FROM sessions WHERE id=?", (session_id,)).fetchone()
        epoch = conn.execute("SELECT value FROM user_profile WHERE key='dataset_epoch'").fetchone()
        try:
            matches_epoch = bool(epoch and json.loads(epoch[0]).get('value') == dataset_epoch)
        except (ValueError, TypeError, AttributeError):
            matches_epoch = False
        if not owner or owner[0] != avatar_id or not matches_epoch:
            raise ValueError('The conversation or companion changed. Reload and review the picture request.')

    def check_shared_photo_scope(self, session_id, avatar_id, dataset_epoch):
        if (type(session_id) is not int or not 0 < session_id < 2 ** 63
                or not isinstance(avatar_id, str) or not avatar_id
                or not isinstance(dataset_epoch, str) or not dataset_epoch):
            raise ValueError('The shared picture context is invalid.')
        conn = self._connect()
        try:
            self._check_shared_photo_scope(conn, session_id, avatar_id, dataset_epoch)
        finally:
            conn.close()

    def require_new_shared_photo(self, session_id, avatar_id, dataset_epoch, request_id):
        """Refuse a missing image whose prior request was already recorded."""
        if (type(session_id) is not int or not 0 < session_id < 2 ** 63
                or not isinstance(avatar_id, str) or not avatar_id or not isinstance(dataset_epoch, str) or not dataset_epoch
                or not isinstance(request_id, str) or not re.fullmatch(r'shared_[0-9a-f]{32}', request_id)):
            raise ValueError('The shared picture context is invalid.')
        conn = self._connect()
        try:
            self._check_shared_photo_scope(conn, session_id, avatar_id, dataset_epoch)
            if conn.execute('SELECT 1 FROM chat_request_tombstones WHERE request_id=?', ('photo:' + request_id,)).fetchone():
                raise ValueError('This shared picture was removed. Start a new request.')
        finally:
            conn.close()

    def record_shared_photo(self, session_id, avatar_id, dataset_epoch, request_id, photo_id):
        """Atomically bind one reviewed shared-photo marker to its original chat.

        A retained opaque tombstone prevents a lost-response retry from
        recreating a marker the user later deleted or edited. This records no
        reference bytes or source image metadata.
        """
        if (type(session_id) is not int or not 0 < session_id < 2 ** 63
                or not isinstance(avatar_id, str) or not avatar_id
                or not isinstance(dataset_epoch, str) or not dataset_epoch
                or not isinstance(request_id, str) or not re.fullmatch(r'shared_[0-9a-f]{32}', request_id)
                or not isinstance(photo_id, str) or not re.fullmatch(r'p-[0-9a-f]{10}', photo_id)):
            raise ValueError('The shared picture context is invalid.')
        key, marker = 'photo:' + request_id, f'[[photo:{photo_id}]]'
        conn = self._connect()
        try:
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                self._check_shared_photo_scope(conn, session_id, avatar_id, dataset_epoch)
                retired = conn.execute('SELECT 1 FROM chat_request_tombstones WHERE request_id=?', (key,)).fetchone()
                existing = conn.execute("SELECT id FROM messages WHERE session_id=? AND role='assistant' AND content=?",
                                        (session_id, marker)).fetchall()
                if retired:
                    if len(existing) != 1:
                        raise ValueError('This shared picture message was changed or removed. Start a new request.')
                    return existing[0]['id']
                if existing or conn.execute('SELECT 1 FROM chat_turns WHERE request_id=?', (key,)).fetchone():
                    raise ValueError('The shared picture request could not be bound safely.')
                cursor = conn.execute("INSERT INTO messages (session_id,role,content) VALUES (?,'assistant',?)",
                                      (session_id, marker))
                conn.execute('INSERT INTO chat_request_tombstones(request_id) VALUES(?)', (key,))
                conn.execute('UPDATE sessions SET updated_at=CURRENT_TIMESTAMP WHERE id=?', (session_id,))
                return cursor.lastrowid
        finally:
            conn.close()

    def get_messages_after(self, session_id: int, message_id: int) -> list[MessageRecord]:
        """Get messages after a specific message ID in a session.

        Args:
            session_id: The session to fetch from.
            message_id: Get messages with id > this value.

        Returns:
            List of MessageRecord objects, oldest first (for LLM context ordering).
        """
        conn = self._connect()
        try:
            cursor = conn.execute(
                "SELECT id, session_id, role, content, mood, timestamp FROM messages WHERE session_id = ? AND id > ? ORDER BY id ASC",
                (session_id, message_id)
            )
            records = []
            for row in cursor.fetchall():
                mood_data = None
                if row["mood"]:
                    try:
                        mood_data = json.loads(row["mood"])
                    except (json.JSONDecodeError, TypeError):
                        pass

                records.append(MessageRecord(
                    id=row["id"],
                    session_id=row["session_id"],
                    role=row["role"],
                    content=row["content"],
                    mood=mood_data,
                    timestamp=row["timestamp"] or ""
                ))
            return records
        finally:
            conn.close()

    def append_imported_messages(self, session_id: int, messages: list[tuple[str, str]]) -> int:
        """Append a validated import in one transaction, or leave the conversation unchanged."""
        if not messages or any(role not in {'user', 'assistant'} or not isinstance(content, str) or not content.strip()
                               for role, content in messages):
            raise ValueError('The import contains an empty or invalid message.')
        conn = self._connect()
        try:
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                if not conn.execute('SELECT 1 FROM sessions WHERE id=?', (session_id,)).fetchone():
                    raise LookupError('Session not found')
                if conn.execute("SELECT 1 FROM chat_turns WHERE session_id=? AND status IN ('sending','streaming')", (session_id,)).fetchone():
                    raise ValueError('Wait for the active reply to finish before importing messages.')
                conn.executemany('INSERT INTO messages(session_id,role,content) VALUES(?,?,?)',
                                 ((session_id, role, content) for role, content in messages))
                conn.execute('UPDATE sessions SET updated_at=CURRENT_TIMESTAMP WHERE id=?', (session_id,))
            return len(messages)
        finally:
            conn.close()

    def get_messages(self, session_id: int, limit: int = 15) -> list[MessageRecord]:
        """Get recent messages for a session.

        Args:
            session_id: The session to fetch messages from.
            limit: Maximum number of messages (default 15).

        Returns:
            List of MessageRecord objects, oldest first (for LLM context ordering).
        """
        conn = self._connect()
        try:
            cursor = conn.execute(
                "SELECT id, session_id, role, content, mood, timestamp FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                (session_id, limit)
            )
            records = []
            for row in cursor.fetchall():
                mood_data = None
                if row["mood"]:
                    try:
                        mood_data = json.loads(row["mood"])
                    except (json.JSONDecodeError, TypeError):
                        pass

                records.append(MessageRecord(
                    id=row["id"],
                    session_id=row["session_id"],
                    role=row["role"],
                    content=row["content"],
                    mood=mood_data,
                    timestamp=row["timestamp"] or ""
                ))

            # Reverse for oldest-first ordering (LLM context)
            records.reverse()
            return records
        finally:
            conn.close()

    def get_recent_messages_across_sessions(self, limit: int = 20) -> list[dict]:
        """Get recent messages across all sessions (for proactive check-ins).

        Args:
            limit: Maximum number of messages to return.

        Returns:
            List of dicts with keys: id, session_id, role, content, mood, timestamp.
        """
        conn = self._connect()
        try:
            cursor = conn.execute(
                "SELECT id, session_id, role, content, mood, timestamp FROM messages ORDER BY id DESC LIMIT ?",
                (limit,)
            )
            result = []
            for row in cursor.fetchall():
                mood_data = None
                if row["mood"]:
                    try:
                        mood_data = json.loads(row["mood"])
                    except (json.JSONDecodeError, TypeError):
                        pass
                result.append({
                    "id": row["id"],
                    "session_id": row["session_id"],
                    "role": row["role"],
                    "content": row["content"],
                    "mood": mood_data,
                    "timestamp": row["timestamp"] or ""
                })
            # Reverse for oldest-first ordering
            result.reverse()
            return result
        finally:
            conn.close()

    def get_user_messages(self, session_id: int, limit: int = 15) -> list[str]:
        """Get just the text of user messages in a session.

        Args:
            session_id: The session to fetch from.
            limit: Maximum number of messages.

        Returns:
            List of message content strings (user role only).
        """
        conn = self._connect()
        try:
            cursor = conn.execute(
                "SELECT content FROM messages WHERE session_id = ? AND role = 'user' ORDER BY id DESC LIMIT ?",
                (session_id, limit)
            )
            return [row["content"] for row in cursor.fetchall()]
        finally:
            conn.close()

    def get_recent_user_context(self, session_id: int, limit: int = 5) -> list[str]:
        """Get recent user message content for memory search context.

        Args:
            session_id: The session to fetch from.
            limit: Maximum number of messages (default 5).

        Returns:
            List of message content strings, most recent first.
        """
        conn = self._connect()
        try:
            cursor = conn.execute(
                "SELECT content FROM messages WHERE session_id = ? AND role = 'user' ORDER BY id DESC LIMIT ?",
                (session_id, limit)
            )
            return [row["content"] for row in cursor.fetchall()]
        finally:
            conn.close()

    def delete_messages_by_session(self, session_id: int) -> None:
        """Delete all messages from a session."""
        conn = self._connect()
        try:
            conn.execute("DELETE FROM message_version_current WHERE message_id IN (SELECT id FROM messages WHERE session_id = ?)", (session_id,))
            conn.execute("DELETE FROM message_versions WHERE message_id IN (SELECT id FROM messages WHERE session_id = ?)", (session_id,))
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            conn.commit()
        finally:
            conn.close()

    def delete_messages_after(self, session_id: int, message_id: int) -> None:
        """Delete messages after a specific ID in a session."""
        conn = self._connect()
        try:
            conn.execute('BEGIN IMMEDIATE')
            self._retire_session_requests(conn, [session_id])
            self._invalidate_session_knowledge(conn, session_id)
            conn.execute(
                "DELETE FROM message_version_current WHERE message_id IN (SELECT id FROM messages WHERE session_id = ? AND id > ?)",
                (session_id, message_id)
            )
            conn.execute(
                "DELETE FROM message_versions WHERE message_id IN (SELECT id FROM messages WHERE session_id = ? AND id > ?)",
                (session_id, message_id)
            )
            conn.execute(
                "DELETE FROM messages WHERE session_id = ? AND id > ?",
                (session_id, message_id)
            )
            conn.commit()
        finally:
            conn.close()

    def get_message_count(self, session_id: int) -> int:
        """Get the number of messages in a session."""
        conn = self._connect()
        try:
            cursor = conn.execute(
                "SELECT COUNT(*) as cnt FROM messages WHERE session_id = ?",
                (session_id,)
            )
            return cursor.fetchone()["cnt"]
        finally:
            conn.close()

    # ----------------------------------------------------------
    # Life Events Management
    # ----------------------------------------------------------

    def add_life_event(self, session_id: int, event_type: str, detail: str,
                       confidence: float = 0.7, raw_text: str = "") -> int:
        """Add a life event to the timeline.

        Args:
            session_id: Source session where the event was detected.
            event_type: One of the valid event types.
            detail: Brief description (truncated to 500 chars).
            confidence: Extraction confidence score (0-1).
            raw_text: Original text snippet (truncated to 200 chars).

        Returns:
            The new event's ID.
        """
        conn = self._connect()
        try:
            cursor = conn.execute(
                "INSERT INTO life_events (session_id, event_type, detail, confidence, raw_text) VALUES (?, ?, ?, ?, ?)",
                (session_id, event_type, detail[:500], confidence, raw_text[:200])
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()

    def get_events(self, limit: int = 50) -> list[LifeEvent]:
        """Get life events ordered by timestamp.

        Args:
            limit: Maximum number of events (default 50).

        Returns:
            List of LifeEvent objects sorted by timestamp DESC.
        """
        conn = self._connect()
        try:
            cursor = conn.execute(
                "SELECT id, session_id, event_type, detail, confidence, timestamp, raw_text FROM life_events ORDER BY timestamp DESC LIMIT ?",
                (limit,)
            )
            return [
                LifeEvent(
                    id=row["id"],
                    session_id=row["session_id"],
                    event_type=row["event_type"],
                    detail=row["detail"],
                    confidence=float(row["confidence"]) if row["confidence"] else 0.7,
                    timestamp=row["timestamp"] or "",
                    raw_text=row["raw_text"] or ""
                )
                for row in cursor.fetchall()
            ]
        finally:
            conn.close()

    def get_events_by_type(self, event_type: str) -> list[LifeEvent]:
        """Get life events filtered by type.

        Args:
            event_type: The event type to filter by.

        Returns:
            List of LifeEvent objects sorted by timestamp DESC.
        """
        conn = self._connect()
        try:
            cursor = conn.execute(
                "SELECT id, session_id, event_type, detail, confidence, timestamp, raw_text FROM life_events WHERE event_type = ? ORDER BY timestamp DESC",
                (event_type,)
            )
            return [
                LifeEvent(
                    id=row["id"],
                    session_id=row["session_id"],
                    event_type=row["event_type"],
                    detail=row["detail"],
                    confidence=float(row["confidence"]) if row["confidence"] else 0.7,
                    timestamp=row["timestamp"] or "",
                    raw_text=row["raw_text"] or ""
                )
                for row in cursor.fetchall()
            ]
        finally:
            conn.close()

    def get_event_summary(self) -> dict:
        """Get a summary of life events grouped by type.

        Returns:
            Dict mapping event_type to {count, latest_date, earliest_date}.
        """
        conn = self._connect()
        try:
            cursor = conn.execute(
                "SELECT event_type, COUNT(*) as count, MAX(timestamp) as latest, MIN(timestamp) as earliest FROM life_events GROUP BY event_type"
            )
            summary = {}
            for row in cursor.fetchall():
                summary[row["event_type"]] = {
                    "count": row["count"],
                    "latest_date": row["latest"] or "",
                    "earliest_date": row["earliest"] or ""
                }
            return summary
        finally:
            conn.close()

    def clear_all_events(self) -> None:
        """Delete all life events (admin operation)."""
        conn = self._connect()
        try:
            conn.execute("DELETE FROM life_events")
            conn.commit()
        finally:
            conn.close()

    # ----------------------------------------------------------
    # Admin / Utility Operations
    # ----------------------------------------------------------

    def clear_all_messages(self) -> None:
        """Delete all messages (admin operation)."""
        conn = self._connect()
        try:
            conn.execute("DELETE FROM message_version_current")
            conn.execute("DELETE FROM message_versions")
            conn.execute("DELETE FROM messages")
            conn.commit()
        finally:
            conn.close()

    def reset_all_user_data(self) -> None:
        """Delete conversation, session, event, search, and profile data."""
        conn = self._connect()
        try:
            if conn.execute("SELECT 1 FROM chat_turns WHERE status IN ('sending','streaming')").fetchone():
                raise ValueError("Wait for replies to finish before clearing data.")
            conn.execute("INSERT OR IGNORE INTO chat_request_tombstones(request_id) SELECT request_id FROM chat_turns")
            conn.execute("DELETE FROM voice_notes")
            conn.execute("DELETE FROM message_version_current")
            conn.execute("DELETE FROM message_versions")
            for table in ('reflection_notebook', 'reflection_reports', 'reflection_jobs', 'reflection_preferences',
                          'episode_recall_reservations', 'episode_recall_turns', 'companion_episodes',
                          'presence_inbox', 'presence_jobs', 'presence_rituals', 'presence_preferences', 'companion_expression'):
                conn.execute('DELETE FROM ' + table)
            conn.execute("DELETE FROM chat_turn_events")
            conn.execute("DELETE FROM chat_turns")
            conn.execute("DELETE FROM messages")
            conn.execute("DELETE FROM sessions")
            conn.execute("DELETE FROM life_events")
            conn.execute("DELETE FROM user_profile")
            # The reset and stale-tab barrier must commit together, even if a later write fails.
            conn.execute("INSERT INTO user_profile(key,value) VALUES('dataset_epoch',?)",
                         (json.dumps({'value': uuid.uuid4().hex, 'required': True}),))
            try:
                conn.execute("INSERT INTO messages_fts(messages_fts) VALUES ('delete-all')")
            except sqlite3.Error:
                pass
            conn.commit()
        finally:
            conn.close()

    def set_feature_flag(self, flag_key: str, enabled: bool) -> None:
        """Set a feature flag in user_profile (legacy pattern)."""
        value = "true" if enabled else "false"
        conn = self._connect()
        try:
            cursor = conn.execute(
                "SELECT key FROM user_profile WHERE key = ?", (flag_key,)
            )
            existing = cursor.fetchone()

            if existing:
                conn.execute(
                    "UPDATE user_profile SET value=?, updated_at=CURRENT_TIMESTAMP WHERE key=?",
                    (value, flag_key)
                )
            else:
                conn.execute(
                    "INSERT INTO user_profile (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
                    (flag_key, value)
                )
            conn.commit()
        finally:
            conn.close()

    # ----------------------------------------------------------
    # Message Mood Updates
    # ----------------------------------------------------------

    def update_message_mood(self, session_id: int, role: str, mood_data: dict) -> bool:
        """Update the mood field on the most recent message for a session/role.

        Args:
            session_id: The session to look in.
            role: 'user' or 'assistant'.
            mood_data: Dict of emotion data (will be JSON-encoded).

        Returns:
            True if a message was found and updated, False otherwise.
        """
        conn = self._connect()
        try:
            cursor = conn.execute(
                "SELECT id FROM messages WHERE session_id = ? AND role = ? ORDER BY id DESC LIMIT 1",
                (session_id, role)
            )
            row = cursor.fetchone()

            if row:
                msg_id = row[0]
                conn.execute(
                    "UPDATE messages SET mood = ? WHERE id = ?",
                    (json.dumps(mood_data), msg_id)
                )
                conn.commit()
                return True
            return False
        finally:
            conn.close()

    # ----------------------------------------------------------
    # FTS5 Search Management
    # ----------------------------------------------------------

    def ensure_fts_table(self, fts_table_name: str = "messages_fts") -> None:
        """Ensure the FTS5 index exists, is kept in step with messages, and has been filled in at least once.

        messages_fts is an *external content* table (content='messages'): it holds only the index, and reads the words
        themselves back out of messages. Two things follow, and getting either wrong leaves search silently empty.

          * Nothing writes to the index on its own. The three triggers below do it, so a message is searchable the moment
            it is saved. Without them the index stays as empty as the day it was created.
          * COUNT(*) on the index is answered by messages, so it can never be used to tell whether the index is filled.
            (It was, and the rebuild it guarded therefore never ran once.) The one-time backfill is recorded in fts_state
            instead, and does its work with FTS5's own 'rebuild' command rather than a row-by-row loop.

        Args:
            fts_table_name: Name of the FTS5 virtual table to create/sync.
        """
        conn = self._connect()
        try:
            conn.executescript(fts_schema_sql(fts_table_name))
            already_built = conn.execute(
                "SELECT 1 FROM fts_state WHERE name = ?", (fts_table_name,)
            ).fetchone()
            if not already_built:
                conn.execute(f"INSERT INTO {fts_table_name}({fts_table_name}) VALUES ('rebuild')")
                conn.execute(
                    "INSERT OR REPLACE INTO fts_state (name, built_at) VALUES (?, CURRENT_TIMESTAMP)",
                    (fts_table_name,)
                )
            conn.commit()
        finally:
            conn.close()

    def rebuild_fts_index(self, fts_table_name: str = "messages_fts") -> int:
        """Rebuild the search index from the messages table. Returns how many messages are now indexed.

        The triggers keep the index current, so this is only needed after messages are changed behind their back
        (a restored database, a hand-edited row) or to recover from an index that was left half-written.
        """
        conn = self._connect()
        try:
            conn.executescript(fts_schema_sql(fts_table_name))
            conn.execute(f"INSERT INTO {fts_table_name}({fts_table_name}) VALUES ('rebuild')")
            conn.execute(
                "INSERT OR REPLACE INTO fts_state (name, built_at) VALUES (?, CURRENT_TIMESTAMP)",
                (fts_table_name,)
            )
            conn.commit()
            return conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        finally:
            conn.close()

    def fts_search(self, query: str, fts_table_name: str = "messages_fts",
                   session_id=None, mood=None, date_from=None, date_to=None,
                   role=None, limit=50) -> list[dict]:
        """Full-text search across messages using FTS5.

        Args:
            query: Search text (FTS5 full-text search).
            fts_table_name: Name of the FTS5 virtual table to search.
            session_id: Filter by specific session.
            mood: Filter by message mood.
            date_from: Start date filter (ISO format string or datetime).
            date_to: End date filter (ISO format string or datetime).
            role: Filter by role ('user' or 'assistant').
            limit: Maximum results to return.

        Returns:
            List of result dicts with message content and metadata.
        """
        fts_query = fts_match_query(query)
        if not fts_query:
            return []

        conn = self._connect()
        try:
            sql = f"SELECT rowid, content, session_id, role, mood, timestamp FROM {fts_table_name} WHERE {fts_table_name} MATCH ?"
            params = [fts_query]

            # Build regular query for filters (FTS5 doesn't support all WHERE clauses well)
            where_clauses = []
            filter_params = list(params)

            if session_id:
                where_clauses.append("session_id = ?")
                filter_params.append(session_id)

            if mood:
                where_clauses.append("mood = ?")
                filter_params.append(mood)

            if role:
                where_clauses.append("role = ?")
                filter_params.append(role)

            if date_from:
                df = date_from
                if hasattr(df, 'isoformat'):
                    df = df.isoformat()
                where_clauses.append("timestamp >= ?")
                filter_params.append(df)

            if date_to:
                dt = date_to
                if hasattr(dt, 'isoformat'):
                    dt = dt.isoformat()
                where_clauses.append("timestamp <= ?")
                filter_params.append(dt)

            if where_clauses:
                sql += " AND " + " AND ".join(where_clauses)

            sql += " ORDER BY timestamp DESC LIMIT ?"
            filter_params.append(limit)

            cursor = conn.execute(sql, filter_params)
            results = [dict(row) for row in cursor.fetchall()]

            # Calculate relevance score based on FTS rank (higher = more relevant)
            for result in results:
                result["relevance"] = 1.0  # FTS5 handles ranking internally

            return results
        finally:
            conn.close()

    def search_by_mood(self, mood: str, limit: int = 20) -> list[dict]:
        """Search messages by mood type.

        Args:
            mood: The mood value to filter by.
            limit: Maximum results to return.

        Returns:
            List of result dicts with message content and metadata.
        """
        conn = self._connect()
        try:
            cursor = conn.execute(
                "SELECT rowid, content, session_id, role, mood, timestamp FROM messages WHERE mood = ? ORDER BY timestamp DESC LIMIT ?",
                (mood, limit)
            )
            return [dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    def search_by_session(self, session_id: int, query=None, limit: int = 100) -> list[dict]:
        """Search within a specific session.

        Args:
            session_id: The session to search within.
            query: Optional text to match (uses LIKE).
            limit: Maximum results to return.

        Returns:
            List of result dicts with message content and metadata.
        """
        conn = self._connect()
        try:
            if query:
                cursor = conn.execute(
                    "SELECT rowid, content, session_id, role, mood, timestamp FROM messages WHERE session_id = ? AND content LIKE ? ORDER BY id DESC LIMIT ?",
                    (session_id, f"%{query}%", limit)
                )
            else:
                cursor = conn.execute(
                    "SELECT rowid, content, session_id, role, mood, timestamp FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                    (session_id, limit)
                )
            return [dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    def get_search_stats(self, fts_table_name: str = "messages_fts") -> dict:
        """Get statistics about searchable content.

        Args:
            fts_table_name: Name of the FTS5 virtual table.

        Returns:
            Dict with total_messages, total_sessions, mood_distribution, role_distribution.
        """
        conn = self._connect()
        try:
            total_messages = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            total_sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]

            # Count by mood
            moods = {}
            for row in conn.execute(
                "SELECT mood, COUNT(*) as count FROM messages WHERE mood IS NOT NULL GROUP BY mood"
            ).fetchall():
                moods[row["mood"]] = row["count"]

            # Count by role
            roles = {}
            for row in conn.execute(
                "SELECT role, COUNT(*) as count FROM messages GROUP BY role"
            ).fetchall():
                roles[row["role"]] = row["count"]

            return {
                "total_messages": total_messages,
                "total_sessions": total_sessions,
                "mood_distribution": moods,
                "role_distribution": roles,
            }
        finally:
            conn.close()


# ============================================================
# Schema Management (init_db moved here from altera.py — Session 5)
# ============================================================

SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS chat_request_tombstones (
        request_id TEXT PRIMARY KEY,
        deleted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS chat_turns (
        request_id TEXT PRIMARY KEY,
        session_id INTEGER NOT NULL REFERENCES sessions(id),
        fingerprint TEXT NOT NULL,
        process_id TEXT NOT NULL,
        status TEXT NOT NULL,
        response TEXT NOT NULL DEFAULT '',
        error TEXT NOT NULL DEFAULT '',
        user_message_id INTEGER,
        assistant_message_id INTEGER,
        target_message_id INTEGER,
        first_text_ms INTEGER,
        elapsed_ms INTEGER,
        diagnostics TEXT NOT NULL DEFAULT '{}',
        expression_pending TEXT,
        memory_pending TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_turn ON chat_turns(session_id)
        WHERE status IN ('sending','streaming');
    CREATE TABLE IF NOT EXISTS chat_turn_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        request_id TEXT NOT NULL REFERENCES chat_turns(request_id),
        event TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_turn_events ON chat_turn_events(request_id,id);
    CREATE TABLE IF NOT EXISTS sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT DEFAULT 'New Conversation',
        tags TEXT DEFAULT '',
        archived BOOLEAN DEFAULT 0,
        avatar_id TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER REFERENCES sessions(id),
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        mood TEXT,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS life_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER REFERENCES sessions(id),
        event_type TEXT NOT NULL,
        detail TEXT NOT NULL,
        confidence REAL DEFAULT 0.7,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        raw_text TEXT
    );

    CREATE TABLE IF NOT EXISTS user_profile (
        key TEXT PRIMARY KEY,
        value TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
"""


def fts_match_query(text: str) -> str:
    """What the user typed, as an FTS5 MATCH expression that cannot be a syntax error. '' when there is nothing to search for.

    FTS5's MATCH takes a query language, not plain text: a stray quote, bracket or bare AND raises OperationalError, and a
    search box that answers `"` with a 500 is not a search box. Each word is quoted instead, which makes it a literal term,
    and the words are ANDed (FTS5's default) — "all of these words", which is what someone typing into a search box means.
    """
    words = re.findall(r"[^\s\"]+", text or "")
    return " ".join(f'"{word}"' for word in words)


def fts_schema_sql(fts_table_name: str = "messages_fts") -> str:
    """The FTS5 index for conversation search, plus the triggers that keep it in step with messages.

    An external content table (content='messages') stores only the index and reads the text back out of messages, so a
    message is invisible to search until something writes it into the index. These triggers are that something: every
    insert, update and delete on messages is mirrored here, which is what makes a message searchable as soon as it is
    said. fts_state records that the one-time backfill of pre-existing messages has been done.
    """
    return f"""
    CREATE VIRTUAL TABLE IF NOT EXISTS {fts_table_name} USING fts5(
        content,
        session_id,
        role,
        mood,
        timestamp,
        content='messages',
        content_rowid='id'
    );

    CREATE TABLE IF NOT EXISTS fts_state (
        name TEXT PRIMARY KEY,
        built_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TRIGGER IF NOT EXISTS {fts_table_name}_ai AFTER INSERT ON messages BEGIN
        INSERT INTO {fts_table_name}(rowid, content, session_id, role, mood, timestamp)
        VALUES (new.id, new.content, new.session_id, new.role, new.mood, new.timestamp);
    END;

    CREATE TRIGGER IF NOT EXISTS {fts_table_name}_ad AFTER DELETE ON messages BEGIN
        INSERT INTO {fts_table_name}({fts_table_name}, rowid, content, session_id, role, mood, timestamp)
        VALUES ('delete', old.id, old.content, old.session_id, old.role, old.mood, old.timestamp);
    END;

    CREATE TRIGGER IF NOT EXISTS {fts_table_name}_au AFTER UPDATE ON messages BEGIN
        INSERT INTO {fts_table_name}({fts_table_name}, rowid, content, session_id, role, mood, timestamp)
        VALUES ('delete', old.id, old.content, old.session_id, old.role, old.mood, old.timestamp);
        INSERT INTO {fts_table_name}(rowid, content, session_id, role, mood, timestamp)
        VALUES (new.id, new.content, new.session_id, new.role, new.mood, new.timestamp);
    END;
"""


# Earlier answers kept when an answer is regenerated. The message row keeps its id, because memories, voice notes, episodes
# and the reply ledger all point at that id; only its content is replaced. The message's own id is always one version, and
# every other answer it has had lives here, one row each. Version ids belong to their message: (message_id, version_id) is
# the key, so one message's versions can never take another's. Editing or deleting a message by hand keeps no old text:
# that is a correction, not another reply. `message_version_current` names the version the row is showing, or has no row
# when it is showing its own text (the usual case).
VERSION_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS message_versions (
    message_id INTEGER NOT NULL REFERENCES messages(id),
    version_id INTEGER NOT NULL,
    content TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (message_id, version_id)
);
CREATE TABLE IF NOT EXISTS message_version_current (
    message_id INTEGER PRIMARY KEY REFERENCES messages(id),
    version_id INTEGER NOT NULL,
    FOREIGN KEY (message_id, version_id) REFERENCES message_versions(message_id, version_id)
);
"""


def _ensure_version_schema(conn) -> None:
    """Create the version tables, or move a database made by the first build of alternate replies (one version_id key
    shared by every message, so a regeneration could overwrite another message's saved answer) to the per-message key."""
    keys = [row[1] for row in conn.execute("PRAGMA table_info(message_versions)") if row[5]]
    if keys and keys != ["message_id", "version_id"]:
        conn.execute("ALTER TABLE message_versions RENAME TO message_versions_shared_key")
        conn.execute("ALTER TABLE message_version_current RENAME TO message_version_current_shared_key")
        conn.executescript(VERSION_SCHEMA_SQL)
        conn.execute("INSERT OR IGNORE INTO message_versions(message_id, version_id, content, created_at) "
                     "SELECT message_id, version_id, content, created_at FROM message_versions_shared_key")
        conn.execute("INSERT OR IGNORE INTO message_version_current(message_id, version_id) "
                     "SELECT c.message_id, c.version_id FROM message_version_current_shared_key c "
                     "JOIN message_versions v ON v.message_id = c.message_id AND v.version_id = c.version_id")
        conn.execute("DROP TABLE message_version_current_shared_key")
        conn.execute("DROP TABLE message_versions_shared_key")
        conn.execute("DROP INDEX IF EXISTS idx_message_versions")
    else:
        conn.executescript(VERSION_SCHEMA_SQL)


def _start_memory_reading_after_existing_history(conn) -> None:
    """Once per database: when the model memory writer first meets a database that already has chats (an upgrade), each
    person's read point (`memory_scan:<owner>`, see core.memory.MemoryWriter) starts after the messages they already have.
    The phrase matcher read those when they were sent, and Rebuild on the Memory page has the model read them again when the
    person asks. Without this, the first reply after an upgrade would send every message ever written to the model. A new
    database has no messages yet, so its people are read from their first message."""
    if conn.execute("SELECT 1 FROM user_profile WHERE key='memory_writer_started'").fetchone():
        return
    rows = conn.execute("SELECT COALESCE(s.avatar_id, ''), MAX(m.id) FROM messages m JOIN sessions s ON s.id = m.session_id "
                        "WHERE m.role = 'user' GROUP BY COALESCE(s.avatar_id, '')").fetchall()
    for owner, latest in rows:
        if latest:
            conn.execute("INSERT OR IGNORE INTO user_profile(key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
                         ("memory_scan:" + (owner or "@default"), json.dumps({"through": int(latest)})))
    conn.execute("INSERT OR IGNORE INTO user_profile(key, value, updated_at) VALUES ('memory_writer_started', ?, "
                 "CURRENT_TIMESTAMP)", (json.dumps({"existing_people": len(rows)}),))


def init_db(db_path=None) -> None:
    """Initialize the database with required tables and schema migrations.

    Args:
        db_path: Path to the SQLite database file.
    """
    import sqlite3
    if db_path is None:
        from core.paths import data_path
        db_path = str(data_path('conversations.db'))

    os.makedirs(os.path.dirname(db_path) if os.path.dirname(db_path) else ".", exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        # WAL is persistent for this database. Select it before publishing the
        # initialized schema, rather than changing journal mode on an ordinary
        # query while another reader/writer may already hold a transaction.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA_SQL)
        _ensure_version_schema(conn)
        from core.voice_notes import ensure_schema as ensure_voice_notes_schema
        ensure_voice_notes_schema(conn)
        from core.expression import ensure_schema as ensure_expression_schema
        from core.presence import ensure_schema as ensure_presence_schema
        ensure_expression_schema(conn)
        ensure_presence_schema(conn)
        from core.episodes import ensure_schema as ensure_episode_schema
        from core.reflection import ensure_schema as ensure_reflection_schema
        ensure_episode_schema(conn)
        ensure_reflection_schema(conn)
        try:
            conn.execute("ALTER TABLE chat_turns ADD COLUMN target_message_id INTEGER")
        except sqlite3.OperationalError:
            pass
        columns = {row[1] for row in conn.execute('PRAGMA table_info(chat_turns)')}
        if 'diagnostics' not in columns:
            conn.execute("ALTER TABLE chat_turns ADD COLUMN diagnostics TEXT NOT NULL DEFAULT '{}'")
        if 'expression_pending' not in columns:
            conn.execute('ALTER TABLE chat_turns ADD COLUMN expression_pending TEXT')
        if 'memory_pending' not in columns:
            conn.execute('ALTER TABLE chat_turns ADD COLUMN memory_pending TEXT')

        # Add archived column to sessions if it doesn't exist (for existing databases)
        try:
            conn.execute("ALTER TABLE sessions ADD COLUMN archived BOOLEAN DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # Column already exists

        try:
            conn.execute("ALTER TABLE sessions ADD COLUMN tags TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass  # Column already exists

        # Which companion (avatar) a chat belongs to
        try:
            conn.execute("ALTER TABLE sessions ADD COLUMN avatar_id TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass  # Column already exists
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_avatar ON sessions(avatar_id)")
        # Every message query filters by session and reads in id order
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id)")
        # After the column migrations above: it reads sessions.avatar_id, which an old database gains only there.
        _start_memory_reading_after_existing_history(conn)

        # The search index and the triggers that keep it current (see fts_schema_sql)
        conn.executescript(fts_schema_sql())

        conn.commit()
    finally:
        conn.close()


# ============================================================
# Module-level singleton (for easy import from core modules)
# ============================================================

_datastore_instance = None


def get_datastore(db_path=None) -> DataStore:
    """Get or create the shared DataStore instance.

    Args:
        db_path: Path to the SQLite database file.

    Returns:
        Shared DataStore singleton.
    """
    global _datastore_instance
    if _datastore_instance is None:
        _datastore_instance = DataStore(db_path)
    return _datastore_instance
