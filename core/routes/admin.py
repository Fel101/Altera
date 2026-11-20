"""Admin, privacy controls, and offline/PWA routes extracted from altera.py (Session 6: Blueprints).

Routes for data clearing, offline message queueing, and PWA install prompts.
All other admin-related routes have been split into separate blueprint modules:
- relationships.py: Relationship graph management
- personality.py: Personality evolution & templates
- events.py: Life event timeline extraction
- search_export.py: Search, export, knowledge summary, memory dedup
- jokes_opinions.py: Jokes, opinions, and humor style analysis
"""

from core.paths import data_path
import os
import json
import logging
import threading
import uuid
from datetime import datetime
from contextlib import closing
from flask import Blueprint, request, jsonify, current_app, g
from core.constants import OFFLINE_QUEUE_MAX_SIZE
from core.state_gate import STATE, StateBusy

bp = Blueprint('admin', __name__)
LOGGER = logging.getLogger(__name__)

# Resolve project root for config/personality file access (avoids circular import with altera.py)
_BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


# ============================================================
# Routes — Privacy Controls (Phase 5)
# ============================================================

@bp.route("/api/clear-data", methods=["POST"])
def clear_all_data():
    """Delete all conversations, memories, profile state, and derived data."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or data.get("confirm") != "delete-all":
        return jsonify({"error": "Confirmation required: send {\"confirm\": \"delete-all\"}"}), 400

    try:
        from core.datastore import get_datastore
        from core.memory import get_memory_manager
        from core.profile import save_user_profile
        from core import companion, notes, tts

        store = get_datastore()
        # Reserve the whole operation before deleting anything. Photo/notes workers also hold leases.
        with STATE.exclusive(allow_lease=getattr(g, 'altera_state_lease', None)):
            with closing(store._connect()) as conn:
                if conn.execute("SELECT 1 FROM chat_turns WHERE status IN ('sending','streaming')").fetchone():
                    raise StateBusy('Wait for replies to finish before clearing data.', 409)
            services = companion.get_services()
            # Use the shared manager so its cached collection is reset along with ChromaDB.
            # Clear auxiliary stores first; failures must not claim the whole operation succeeded.
            get_memory_manager().clear_all_memories()
            services.photos.delete_all()
            tts.clear_cache()
            from core.voice_notes import get_voice_note_service
            get_voice_note_service(store).clear_all()
            store_notes = notes.NotesStore(services.avatars)
            store_notes.clear_all()
            for record in services.avatars.list():
                services.avatars.update_state(record["id"], first_met=None)
            save_user_profile({"name": "", "hobbies": [], "preferences": {}, "mood_history": [], "facts": []})
            with _offline_queue_lock:
                save_offline_queue([])
            store.reset_all_user_data()  # Includes adult consent; retained avatars start fresh.
        return jsonify({"status": "ok", "message": "Conversations, memories, pictures, speech cache, notebooks and profile cleared. Imported companions, backups, logs and workspace files remain."})
    except StateBusy:
        raise
    except Exception:
        LOGGER.exception("Clear-data failed")
        return jsonify(error="Clearing data did not finish. Some data may remain; retry before using Altera.", code="clear_incomplete"), 500


# ============================================================
# Routes — Offline Queue & PWA (Phase 7)
# ============================================================

OFFLINE_QUEUE_FILE = str(data_path('offline_queue.json'))
_offline_queue_lock = threading.Lock()


def load_offline_queue():
    """Load the offline message queue from disk (thread-safe)."""
    try:
        if os.path.exists(OFFLINE_QUEUE_FILE):
            with open(OFFLINE_QUEUE_FILE) as f:
                return json.load(f)
    except Exception:
        LOGGER.exception("Error loading offline queue")
    return []


def save_offline_queue(queue):
    """Save the offline message queue to disk (thread-safe)."""
    os.makedirs(os.path.dirname(OFFLINE_QUEUE_FILE), exist_ok=True)
    tmp_path = OFFLINE_QUEUE_FILE + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(queue, f, indent=2)
    os.replace(tmp_path, OFFLINE_QUEUE_FILE)


@bp.route("/api/offline/status", methods=["GET"])
def offline_status():
    """Check if the server is reachable (for offline detection)."""
    from core.datastore import get_datastore
    return jsonify({
        "online": True,
        "dataset_epoch": get_datastore().dataset_epoch()["value"],
        "queue_count": len(load_offline_queue())
    })


@bp.route("/api/messages/queue", methods=["POST"])
def queue_message():
    """Queue a message for later delivery when online."""
    data = request.json or {}
    from core.routes.core_api import validate_message_input
    import re
    if not isinstance(data, dict):
        return jsonify(error="Expected a JSON object"), 400
    message, error = validate_message_input(data.get("message"))
    if error:
        return jsonify(error[0]), error[1]
    msg_id = data.get("request_id", uuid.uuid4().hex)
    if not isinstance(msg_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{8,100}", msg_id):
        return jsonify(error="Invalid request_id"), 400
    with _offline_queue_lock:
        queue = load_offline_queue()
        for item in queue:
            if item.get("msg_id") == msg_id:
                if item.get("message") != message or item.get("session_id") != data.get("session_id"):
                    return jsonify(error="Request ID already used for a different message"), 409
                return jsonify(status="queued", id=msg_id, note="Already queued")
        if len(queue) >= OFFLINE_QUEUE_MAX_SIZE:
            return jsonify(error="The offline queue is full."), 409
        queue.append({"msg_id": msg_id, "request_id": msg_id, "session_id": data.get("session_id"), "dataset_epoch": data.get("dataset_epoch"),
                      "message": message, "timestamp": datetime.now().isoformat(), "status": "pending"})
        save_offline_queue(queue)
    return jsonify({"status": "queued", "id": msg_id, "queue_count": len(queue)})


@bp.route("/api/messages/queue/retry", methods=["POST"])
def retry_queued_messages():
    """Retry all queued messages. Returns list of results (thread-safe)."""
    with _offline_queue_lock:
        queue = load_offline_queue()
        results = []
        pending_items = [item for item in queue if item.get("status") == "pending"]

        # Older queues had no stable request ID. Commit their IDs before attempting delivery.
        for item in pending_items:
            item.setdefault("request_id", uuid.uuid4().hex)
        save_offline_queue(queue)

        for item in pending_items:
            try:
                from core.chat import get_chat_engine
                from core.turns import get_turn_service
                from core.routes.core_api import validate_message_input
                message, error = validate_message_input(item.get("message"))
                if error:
                    raise ValueError(error[0]["error"])
                def produce(sid, message=message):
                    for piece in get_chat_engine().send_message(message, sid, stream=True):
                        if isinstance(piece, str):
                            yield {"response": piece}
                service = get_turn_service()
                sid = item.get("session_id")
                if sid is not None:
                    sid = int(sid)
                turn = service.submit(item["request_id"], sid, message, False, produce, current_app._get_current_object(),
                                      dataset_epoch=item.get("dataset_epoch"))
                for _ in service.events(turn["request_id"]):
                    pass
                saved = service.get(turn["request_id"])
                if saved["status"] != "saved":
                    raise RuntimeError("The reply was not saved.")
                results.append({"id": item["msg_id"], "status": "sent", "response_len": len(saved["response"])})
                item["status"] = "sent"
            except Exception:
                LOGGER.exception("Offline retry failed for %s", item["msg_id"])
                results.append({"id": item["msg_id"], "status": "failed", "error": "An unexpected error occurred."})

        save_offline_queue([item for item in queue if item.get("status") != "sent"])

    return jsonify({
        "results": results,
        "remaining": len([q for q in queue if q.get("status") == "pending"])
    })


@bp.route("/api/pwa/install-prompt", methods=["GET"])
def pwa_install_prompt():
    """Return install prompt state (for frontend to decide whether to show banner)."""
    return jsonify({
        "prompt_available": False  # Frontend will use beforeinstallprompt event instead
    })
