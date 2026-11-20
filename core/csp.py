"""A Content-Security-Policy for every page Altera renders.

Scripts run only from Altera's own /static files, plus the small start-up scripts in the templates, which carry a nonce
that is new for each page. A script injected into a page some other way (a message, a card, a name that slipped through
escaping) does not run, and neither does an inline event handler: pages name their actions with data-click and similar
attributes instead (static/js/actions.js). Styles may still be inline, because the templates and scripts set style
attributes; a style cannot run code.

Pictures, speech and recordings come from Altera itself or from blob: and data: URLs it makes in the page. Nothing is
loaded from another site, the page cannot be framed by one, and forms post only to Altera.
"""
import secrets

from flask import g

POLICY = ("default-src 'self'; script-src 'self' 'nonce-{nonce}'; style-src 'self' 'unsafe-inline'; "
          "img-src 'self' data: blob:; media-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; "
          "worker-src 'self'; manifest-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; "
          "frame-ancestors 'none'")


def nonce() -> str:
    """This request's nonce, made the first time a template asks for it."""
    if not getattr(g, 'csp_nonce', None):
        g.csp_nonce = secrets.token_urlsafe(18)
    return g.csp_nonce


def install_content_security_policy(app):
    @app.context_processor
    def csp_context():
        return {'csp_nonce': nonce()}

    @app.after_request
    def content_security_policy(response):
        # A response that set its own policy (a stricter one for an uploaded file, say) keeps it.
        if response.mimetype == 'text/html' and 'Content-Security-Policy' not in response.headers:
            response.headers['Content-Security-Policy'] = POLICY.format(nonce=nonce())
        return response
