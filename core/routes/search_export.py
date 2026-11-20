"""Search and export routes extracted from admin.py (Session 6: Blueprints).

Routes for conversation search, data export, knowledge summary, and memory deduplication.
"""

import logging
import json
import os
from datetime import datetime
from flask import Blueprint, request, jsonify, Response
from core.constants import api_error

from core.config import get_personality

LOGGER = logging.getLogger(__name__)

bp = Blueprint('search_export', __name__)


# ============================================================
# Routes — Search & Export (Phase 8)
# ============================================================

@bp.route("/api/search", methods=["GET"])
def conversation_search():
    """Search conversations with filters."""
    try:
        from core.search import conversation_search

        query = request.args.get("q", "").strip()
        session_id = request.args.get("session_id", type=int)
        mood = request.args.get("mood")
        date_from = request.args.get("date_from")
        date_to = request.args.get("date_to")
        role = request.args.get("role")  # 'user' or 'assistant'
        limit = request.args.get("limit", 50, type=int)

        if not query:
            return jsonify({
                "results": [],
                "query": "",
                "stats": conversation_search.get_search_stats()
            })

        results = conversation_search.search(
            query=query,
            session_id=session_id,
            mood=mood,
            date_from=date_from,
            date_to=date_to,
            role=role,
            limit=limit
        )

        stats = conversation_search.get_search_stats()

        return jsonify({
            "results": results,
            "query": query,
            "total_results": len(results),
            "stats": stats
        })
    except Exception:
        LOGGER.exception("Search error")
        return api_error("internal_error", 500)


@bp.route("/api/search/stats", methods=["GET"])
def search_stats():
    """Get search statistics."""
    try:
        from core.search import conversation_search

        stats = conversation_search.get_search_stats()
        return jsonify(stats)
    except Exception:
        LOGGER.exception("Stats error")
        return api_error("internal_error", 500)


@bp.route("/api/export", methods=["POST"])
def export_data():
    """Export all data in specified format."""
    try:
        from core.export import memory_exporter
        from core.datastore import get_datastore

        store = get_datastore()

        data = request.json or {}
        fmt = data.get("format", "json")  # json, markdown, pdf

        if fmt == "markdown":
            content, filename = memory_exporter.export_markdown()
            return Response(
                content.encode("utf-8"),
                mimetype="text/markdown",
                headers={"Content-Disposition": f"attachment; filename={filename}"}
            )

        elif fmt == "pdf":
            success, filepath_or_error = memory_exporter.export_pdf_summary()
            if success:
                with open(filepath_or_error, "rb") as f:
                    content = f.read()

                ext = os.path.splitext(filepath_or_error)[1].lower()
                mimetype = "application/pdf" if ext == ".pdf" else "text/html"

                return Response(
                    content,
                    mimetype=mimetype,
                    headers={"Content-Disposition": f"attachment; filename={os.path.basename(filepath_or_error)}"}
                )
            else:
                return jsonify({"error": filepath_or_error}), 500

        elif fmt == "json":
            from core.datastore import get_datastore

            store = get_datastore()

            sessions = store.get_all_sessions_for_export()

            all_messages = []
            for s in sessions:
                msgs = store.get_messages(s['id'], limit=10000)
                all_messages.extend([{k: v for k, v in m.__dict__.items()} for m in msgs])

            from core.profile import load_user_profile

            profile = load_user_profile()

            memories, warnings = [], []
            try:
                from core.memory import MemoryManager

                mem_mgr = MemoryManager()
                memories = mem_mgr.get_all_memories()
            except Exception:
                LOGGER.exception("Memories could not be read for the export")
                warnings.append("Memories could not be read, so this export has none. Try again, or make a backup.")

            export_data_obj = {
                "exported_at": datetime.now().isoformat(),
                "version": "1.0",
                "sessions": sessions,
                "messages": all_messages,
                "user_profile": profile,
                "memories": memories,
                "personality": get_personality(),
            }
            if warnings:
                export_data_obj["warnings"] = warnings

            json_bytes = json.dumps(export_data_obj, indent=2).encode("utf-8")
            return Response(
                json_bytes,
                mimetype="application/json",
                headers={"Content-Disposition": f"attachment; filename=altera-data-{datetime.now().strftime('%Y-%m-%d')}.json"}
            )

        else:
            return jsonify({"error": f"Unknown format: {fmt}"}), 400

    except Exception:
        LOGGER.exception("Export failed")
        return api_error("internal_error", 500)


@bp.route("/api/knowledge/summary", methods=["GET"])
def knowledge_summary():
    """Get a summary of Altera's knowledge about the user."""
    try:
        from core.memory import get_memory_manager
        from core.events import event_timeline
        from core.relationships import relationship_graph
        from core.profile import load_user_profile

        mem_mgr = get_memory_manager()
        all_memories = mem_mgr.get_all_memories()

        by_type = {}
        for m in all_memories:
            mem_type = m.get("metadata", {}).get("type", "fact")
            by_type[mem_type] = by_type.get(mem_type, 0) + 1

        high_conf = [m for m in all_memories if float(m.get("metadata", {}).get("confidence", 0)) > 0.8]

        events_summary = event_timeline.get_summary()

        graph = relationship_graph.get_graph()
        people_count = len([k for k, v in graph.items() if v.get("type") == "person"])
        places_count = len([k for k, v in graph.items() if v.get("type") == "place"])

        profile = load_user_profile()

        return jsonify({
            "memories": {
                "total": len(all_memories),
                "by_type": by_type,
                "high_confidence": len(high_conf)
            },
            "life_events": events_summary,
            "relationships": {
                "people": people_count,
                "places": places_count,
                "total": len(graph)
            },
            "user_profile": {
                "name": profile.get("name", ""),
                "hobbies": profile.get("hobbies", []),
                "preferences": profile.get("preferences", {})
            }
        })
    except Exception:
        LOGGER.exception("Knowledge summary error")
        return api_error("internal_error", 500)


# ============================================================
# Routes — Memory Dedup (Phase 8)
# ============================================================

@bp.route("/api/memory/dedup", methods=["GET"])
def memory_dedup_status():
    """Find duplicate memories for review."""
    try:
        from core.memory import get_memory_manager

        from core.constants import MAX_DEDUP_MEMORIES

        mem_mgr = get_memory_manager()

        all_memories = mem_mgr.get_all_memories()
        # Comparing memories is inherently quadratic here (a short memory can match a long one that contains it, so
        # no cheap test rules a pair out in advance) and this is a synchronous request. Newest first, capped, and the
        # response says so rather than holding the connection open while the count grows.
        examined = all_memories
        if len(all_memories) > MAX_DEDUP_MEMORIES:
            examined = sorted(all_memories,
                              key=lambda m: (m.get("metadata") or {}).get("created_at", ""),
                              reverse=True)[:MAX_DEDUP_MEMORIES]

        duplicate_groups = mem_mgr.deduplicator.find_duplicates(examined)

        return jsonify({
            "duplicates": duplicate_groups,
            "total_duplicates": len(duplicate_groups),
            "total_memories": len(all_memories),
            "examined": len(examined),
            "truncated": len(examined) < len(all_memories),
        })
    except Exception:
        LOGGER.exception("Dedup error")
        return api_error("internal_error", 500)


@bp.route("/api/memory/dedup/consolidate", methods=["POST"])
def memory_dedup_consolidate():
    """Consolidate duplicate memories."""
    try:
        from core.memory import get_memory_manager

        mem_mgr = get_memory_manager()

        data = request.json or {}
        auto_consolidate = data.get("auto", True)

        if auto_consolidate:
            consolidations = mem_mgr.deduplicator.consolidate_duplicates()
        else:
            all_memories = mem_mgr.get_all_memories()
            duplicate_groups = []
            processed = set()

            by_type = {}
            for m in all_memories:
                mem_type = m.get("metadata", {}).get("type", "fact")
                if mem_type not in by_type:
                    by_type[mem_type] = []
                by_type[mem_type].append(m)

            for mem_type, type_mems in by_type.items():
                for i, m1 in enumerate(type_mems):
                    if m1["id"] in processed:
                        continue

                    group = [m1]

                    for j, m2 in enumerate(type_mems):
                        if i == j or m2["id"] in processed:
                            continue

                        words1 = set(m1.get("content", "").lower().split())
                        words2 = set(m2.get("content", "").lower().split())

                        if not words1 or not words2:
                            continue

                        intersection = len(words1 & words2)
                        union = len(words1 | words2)
                        similarity = intersection / union if union > 0 else 0

                        if m1.get("content", "").lower() in m2.get("content", "").lower():
                            similarity = max(similarity, 0.85)

                        if similarity >= 0.6:
                            group.append(m2)

                    if len(group) >= 2:
                        duplicate_groups.append({
                            "type": mem_type,
                            "memories": group,
                            "similarity_scores": [
                                min(len(set(group[0].get("content", "").lower().split()) &
                                     set(g.get("content", "").lower().split())) /
                                    max(len(set(group[0].get("content", "").lower().split()) |
                                         set(g.get("content", "").lower().split())), 1), 1.0)
                                for g in group[1:]
                            ]
                        })
                        for m in group:
                            processed.add(m["id"])

            consolidations = []

        return jsonify({
            "status": "ok",
            "consolidations": consolidations,
            "message": f"Consolidated {len(consolidations)} duplicate group(s)" if consolidations else "No duplicates found"
        })
    except Exception:
        LOGGER.exception("Consolidate error")
        return api_error("internal_error", 500)
