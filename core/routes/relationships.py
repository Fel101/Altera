"""Relationship graph routes extracted from admin.py (Session 6: Blueprints).

Routes for relationship management, entity tracking, and proximity analysis.
"""

import logging
from flask import Blueprint, request, jsonify
from core.constants import api_error

LOGGER = logging.getLogger(__name__)

bp = Blueprint('relationships', __name__)


# ============================================================
# Routes — Relationship Graph (Phase 4)
# ============================================================

@bp.route("/api/relationships", methods=["GET"])
def get_relationships():
    """Get the full relationship graph."""
    try:
        from core.relationships import relationship_graph

        graph = relationship_graph.get_graph()

        people = {k: v for k, v in graph.items() if v.get("type") == "person"}
        places = {k: v for k, v in graph.items() if v.get("type") == "place"}
        orgs = {k: v for k, v in graph.items() if v.get("type") == "organization"}

        return jsonify({
            "people": people,
            "places": places,
            "organizations": orgs,
            "total": len(graph)
        })
    except Exception:
        LOGGER.exception("Graph fetch failed")
        return api_error("internal_error", 500)


@bp.route("/api/relationships/search", methods=["GET"])
def search_relationships():
    """Search entities by name."""
    query = request.args.get("q", "")

    try:
        from core.relationships import relationship_graph

        results = relationship_graph.search_by_name(query)
        return jsonify(results)
    except Exception:
        LOGGER.exception("Search failed")
        return api_error("internal_error", 500)


@bp.route("/api/relationships/people", methods=["GET"])
def get_people():
    """Get all tracked people."""
    try:
        from core.relationships import relationship_graph

        people = relationship_graph.get_people()
        return jsonify(people)
    except Exception:
        LOGGER.exception("People fetch failed")
        return jsonify({})


@bp.route("/api/relationships/places", methods=["GET"])
def get_places():
    """Get all tracked places."""
    try:
        from core.relationships import relationship_graph

        places = relationship_graph.get_places()
        return jsonify(places)
    except Exception:
        LOGGER.exception("Places fetch failed")
        return jsonify({})


@bp.route("/api/relationships/depth", methods=["GET"])
def relationships_depth():
    """Get relationship depth scores and insights."""
    try:
        from core.relationships import relationship_graph

        graph = relationship_graph.update_depth_scores()
        closest = relationship_graph.get_closest_relationships(min_depth=2.0)
        insights = relationship_graph.get_relationship_insights()

        return jsonify({
            "graph": graph,
            "closest_relationships": closest,
            "insights": insights,
            "total_entities": len(graph)
        })
    except Exception:
        LOGGER.exception("Depth analysis failed")
        return api_error("internal_error", 500)
