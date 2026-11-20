"""Shared browser write protection. Local access needs the same protection as remote access."""
from urllib.parse import urlsplit

from flask import jsonify, request


def install_request_security(app):
    @app.before_request
    def protect_writes():
        # Retain the older endpoint's strict origin boundary for compatibility.
        # It now returns a pointer to the opt-in inbox without inference.
        stateful_read = request.endpoint == "engagement.proactive_checkin" and request.method in {"GET", "HEAD"}
        if request.method in {"GET", "HEAD", "OPTIONS"} and not stateful_read:
            return None
        origin = request.headers.get("Origin")
        try:
            parsed = urlsplit(origin) if origin else None
        except ValueError:
            return jsonify(error="Invalid request origin.", code="cross_site_request"), 403
        # Tailscale Serve terminates TLS; trust its scheme only after the remote-access hook.
        scheme = "https" if request.headers.get("Tailscale-User-Login") else request.scheme
        if (request.headers.get("Sec-Fetch-Site") == "cross-site" or
                (parsed and (parsed.scheme != scheme or parsed.netloc.casefold() != request.host.casefold()))):
            return jsonify(error="Cross-site requests are not allowed.", code="cross_site_request"), 403
        return None
