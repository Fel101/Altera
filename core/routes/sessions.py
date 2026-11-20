"""Session management routes extracted from altera.py (Session 6: Blueprints)."""

import logging
import json
import re
from datetime import datetime
from flask import Blueprint, request, jsonify, Response
from core.constants import api_error
from core.datastore import get_datastore
from core.knowledge import KnowledgeError

LOGGER = logging.getLogger(__name__)

bp = Blueprint('sessions', __name__)


@bp.errorhandler(KnowledgeError)
def knowledge_error(error):
    return jsonify(error=str(error)), error.status


def _get_existing_session_or_404(store, session_id):
    session = store.get_session(session_id)
    if not session:
        return None, (jsonify({"error": "Session not found"}), 404)
    return session, None


def _coerce_int(value, field_name):
    try:
        return int(value), None
    except (TypeError, ValueError):
        return None, (jsonify({"error": f"{field_name} must be an integer"}), 400)


@bp.route("/api/sessions", methods=["GET"])
def list_sessions():
    """List all conversation sessions."""
    include_archived = request.args.get('include_archived', 'false').lower() == 'true'

    store = get_datastore()
    # "include archived" means active ones *and* archived ones — passing the flag straight through as archived=True
    # asked for only the archived ones, so ticking the box made every active conversation disappear from the list.
    # DataStore.list_sessions takes None for "both".
    sessions = store.list_sessions(archived=None if include_archived else False)

    results = []
    for s in sessions:
        tags = []
        if s.tags:
            try:
                tags = json.loads(s.tags)
            except (json.JSONDecodeError, ValueError):
                pass
        results.append({
            "id": s.id,
            "name": s.name,
            "tags": tags,
            "archived": s.archived,
            "created_at": s.created_at,
            "updated_at": s.updated_at
        })

    return jsonify(results)


@bp.route("/api/sessions", methods=["POST"])
def create_new_session():
    """Create a new conversation session."""
    data = request.json or {}
    name = data.get("name", "New Conversation")
    tags = data.get("tags", [])

    store = get_datastore()
    session_id = store.create_session(name)
    if tags:
        store.update_session(session_id, tags=json.dumps(tags))

    return jsonify({"id": session_id, "name": name, "tags": tags})


@bp.route("/api/sessions/<int:session_id>/archive", methods=["POST"])
def archive_session(session_id):
    """Archive or unarchive a conversation session."""
    data = request.json or {}
    is_archived = data.get('archived', True)

    store = get_datastore()
    _, err = _get_existing_session_or_404(store, session_id)
    if err:
        return err

    if is_archived:
        store.archive_session(session_id)
    else:
        store.unarchive_session(session_id)

    return jsonify({"status": "ok", "archived": is_archived})


@bp.route("/api/sessions/<int:session_id>/name", methods=["PUT"])
def update_session_name(session_id):
    """Update the name of a conversation session."""
    data = request.json or {}
    new_name = data.get("name")

    if not new_name:
        return jsonify({"error": "Name is required"}), 400

    store = get_datastore()
    _, err = _get_existing_session_or_404(store, session_id)
    if err:
        return err

    store.rename_session(session_id, new_name)

    return jsonify({"status": "ok", "name": new_name})


@bp.route("/api/sessions/<int:session_id>/tags", methods=["PUT"])
def update_session_tags(session_id):
    """Update the tags of a conversation session."""
    data = request.json or {}
    tags = data.get("tags", [])

    store = get_datastore()
    _, err = _get_existing_session_or_404(store, session_id)
    if err:
        return err

    if not isinstance(tags, list):
        return jsonify({"error": "tags must be a list"}), 400

    store.update_session(session_id, tags=json.dumps(tags))

    return jsonify({"status": "ok", "tags": tags})


@bp.route("/api/sessions/archive/list", methods=["GET"])
def list_archived_sessions():
    """List all archived conversation sessions."""
    store = get_datastore()
    sessions = store.list_sessions(archived=True)

    results = []
    for s in sessions:
        tags = []
        if s.tags:
            try:
                tags = json.loads(s.tags)
            except (json.JSONDecodeError, ValueError):
                pass
        results.append({
            "id": s.id,
            "name": s.name,
            "tags": tags,
            "archived": s.archived,
            "created_at": s.created_at,
            "updated_at": s.updated_at
        })

    return jsonify(results)


@bp.route("/api/sessions/<int:session_id>/merge", methods=["POST"])
def merge_sessions(session_id):
    """Merge another session into this session."""
    data = request.json or {}
    target_session_id = data.get('target_session_id')

    if not target_session_id:
        return jsonify({"error": "target_session_id is required"}), 400

    store = get_datastore()
    _, err = _get_existing_session_or_404(store, session_id)
    if err:
        return err

    target_session_id, err = _coerce_int(target_session_id, "target_session_id")
    if err:
        return err

    if target_session_id == session_id:
        return jsonify({"error": "Cannot merge a session into itself"}), 400

    target, err = _get_existing_session_or_404(store, target_session_id)
    if err:
        return err

    owner = store.get_session(session_id).avatar_id or ''
    if (target.avatar_id or '') != owner:
        return jsonify(error='Conversations belonging to different companions cannot be merged.'), 409

    # Move the messages across rather than copying the first 1000 and deleting them all: the copy was capped at 1000
    # and the delete was not, so merging a longer conversation silently destroyed everything past the cap and still
    # answered {"status": "ok"}. move_messages does it in one UPDATE, so nothing can be dropped in between.
    from core.knowledge import KnowledgeService
    from core.notes import get_notes_service
    with KnowledgeService(store, None, get_notes_service()).changing(owner):
        moved = store.move_messages([target_session_id], session_id, retire_requests=True)
        store.archive_session(target_session_id)

    return jsonify({"status": "ok", "merged_from": target_session_id, "messages_moved": moved})


@bp.route("/api/sessions/<int:session_id>/split", methods=["POST"])
def split_session(session_id):
    """Split a session at a specific message ID."""
    data = request.json or {}
    split_message_id = data.get('split_message_id')

    if not split_message_id:
        return jsonify({"error": "split_message_id is required"}), 400

    store = get_datastore()
    _, err = _get_existing_session_or_404(store, session_id)
    if err:
        return err

    split_message_id, err = _coerce_int(split_message_id, "split_message_id")
    if err:
        return err

    from core.knowledge import KnowledgeService
    from core.notes import get_notes_service
    try:
        with KnowledgeService(store, None, get_notes_service()).changing(store.get_session(session_id).avatar_id or ''):
            new_session_id = store.split_session_at(session_id, split_message_id)
    except ValueError as error:
        from core.knowledge import KnowledgeError
        if isinstance(error, KnowledgeError):
            return jsonify(error=str(error)), error.status
        return jsonify(error=str(error)), 400

    return jsonify({"status": "ok", "new_session_id": new_session_id})


@bp.route("/api/sessions/<int:session_id>", methods=["DELETE"])
def delete_session(session_id):
    """Delete a conversation session."""
    store = get_datastore()
    _, err = _get_existing_session_or_404(store, session_id)
    if err:
        return err

    try:
        # Fence accepted picture and other writers through actual deletion.
        # Use the cleanup-only voice service to avoid unrelated recovery under
        # exclusive admission.
        from core.state_gate import STATE, StateBusy, request_lease
        with STATE.exclusive(allow_lease=request_lease()):
            if STATE.fault:
                raise StateBusy('Altera is updating saved data. Please try again shortly.', 409)
            store.delete_session(session_id, cleanup_only=True)
    except StateBusy as e:
        return jsonify(error=str(e), code='state_busy'), e.status
    except ValueError as e:
        return jsonify(error=str(e)), 409

    return jsonify({"status": "ok"})


@bp.route("/api/sessions/<int:session_id>/export", methods=["GET"])
def export_single_session(session_id):
    """Export a single conversation session as Markdown."""
    store = get_datastore()

    session_info = store.get_session(session_id)
    if not session_info:
        return jsonify({"error": "Session not found"}), 404

    messages = store.get_messages(session_id, limit=-1)

    md_lines = [
        f"# {session_info.name}",
        f"Exported on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"Session created: {session_info.created_at}",
        "",
        "---",
        ""
    ]

    for msg in messages:
        role = "You" if msg.role == 'user' else "Altera"
        time_str = datetime.fromisoformat(msg.timestamp.replace('Z', '+00:00')).strftime('%Y-%m-%d %H:%M') if isinstance(msg.timestamp, str) else "Unknown time"
        md_lines.append(f"### {role} — {time_str}")
        md_lines.append("")
        md_lines.append(msg.content)
        md_lines.append("")
        md_lines.append("---")
        md_lines.append("")

    markdown_content = "\n".join(md_lines)
    filename = f"conversation-{session_id}-{datetime.now().strftime('%Y-%m-%d')}.md"

    return Response(
        markdown_content.encode("utf-8"),
        mimetype="text/markdown",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


@bp.route("/api/sessions/<int:session_id>/import-markdown", methods=["POST"])
def import_markdown_to_session(session_id):
    """Import a markdown file into the current session."""
    store = get_datastore()
    info, err = _get_existing_session_or_404(store, session_id)
    if err:
        return err
    if 'file' not in request.files:
        return jsonify({"error": "No file provided"}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No file selected"}), 400

    try:
        content = file.read().decode('utf-8-sig').replace('\r\n', '\n')
        messages = _markdown_messages(content)
        if not messages:
            return jsonify(error='No conversation messages found. Choose an Altera Markdown export.'), 400
        from core.knowledge import KnowledgeService
        from core.notes import get_notes_service
        with KnowledgeService(store, None, get_notes_service()).changing(info.avatar_id or ''):
            imported = store.append_imported_messages(session_id, messages)
        return jsonify({"status": "ok", "messages_imported": imported})
    except UnicodeDecodeError:
        return jsonify(error='The Markdown file must use UTF-8 text.'), 400
    except KnowledgeError:
        raise
    except LookupError:
        return jsonify(error='Session not found'), 404
    except ValueError as error:
        return jsonify(error=str(error)), 400
    except Exception:
        LOGGER.exception("Markdown import failed")
        return api_error("internal_error", 500)


def _markdown_messages(content):
    # Only conversation headings delimit messages. A horizontal rule or inline '---' is valid message text.
    # Ignore heading-like text inside fenced code blocks.
    headings, fence = [], None
    offset = 0
    for line in content.splitlines(keepends=True):
        marker = re.match(r'^ {0,3}(`{3,}|~{3,})', line)
        if marker:
            run = marker.group(1)
            if fence is None:
                fence = run
            elif run[0] == fence[0] and len(run) >= len(fence):
                fence = None
        if fence is None:
            heading = re.fullmatch(r'### (You|Altera|Her) — (\d{4}-\d\d-\d\d[^\n]*)\n?', line)
            if heading:
                headings.append((offset, offset + len(line), heading.group(1)))
        offset += len(line)
    messages = []
    for index, (_, start, role) in enumerate(headings):
        end = headings[index + 1][0] if index + 1 < len(headings) else len(content)
        text = content[start:end].rstrip('\n')
        # Strip exactly the export separator at the end, leaving separators within the body intact.
        if text.lstrip('\n') == '---':
            text = ''
        elif text.endswith('\n\n---'):
            text = text[:-5].rstrip('\n')
        text = text.strip('\n')
        if not text.strip():
            raise ValueError('The import contains an empty message.')
        messages.append(('user' if role == 'You' else 'assistant', text))
    return messages


@bp.route("/api/sessions/auto-name/<int:session_id>", methods=["POST"])
def auto_name_session(session_id):
    """Auto-generate a descriptive name for the session based on content."""
    store = get_datastore()

    # Get recent user messages
    user_messages = store.get_user_messages(session_id, limit=10)

    if not user_messages:
        return jsonify({"name": "New Conversation"})

    combined_text = " ".join(user_messages[:3])  # Use up to last 3 messages

    # Simple heuristic: take the first unique phrase of 2-4 words
    words = re.findall(r'\b\w+\b', combined_text.lower())

    stop_words = {'the', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for', 'of', 'with', 'by', 'is', 'it', 'i', 'you'}
    topic_candidates = []

    for i in range(len(words) - 3):
        phrase = " ".join(words[i:i+4])
        if len(phrase.split()) >= 2 and not all(w in stop_words for w in phrase.split()):
            meaningful_words = [w for w in phrase.split() if w not in stop_words and len(w) > 2]
            if len(meaningful_words) >= 2:
                topic_candidates.append(phrase.title())
                break

    auto_name = topic_candidates[0] if topic_candidates else "Conversation"

    # Limit length
    if len(auto_name) > 30:
        auto_name = auto_name[:27] + "..."

    store.rename_session(session_id, auto_name)

    return jsonify({"name": auto_name})


@bp.route("/api/sessions/last-active", methods=["GET"])
def get_last_active_session():
    """Get the most recent active session ID."""
    store = get_datastore()
    session_id = store.get_last_active_session()
    if session_id:
        session = store.get_session(session_id)
        if session and not session.archived:
            return jsonify({"session_id": session_id, "name": session.name})
    return jsonify({"session_id": None})
