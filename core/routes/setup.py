"""First-run setup: one tiny model check the setup page can show.

The page itself is rendered by `core.routes.pages`; whether the first run is finished comes from
`/api/onboarding/status`, which already owns that state.
"""
from flask import Blueprint, jsonify

from core.setup import check as check_model

bp = Blueprint('setup', __name__)


@bp.post('/api/setup/check')
def setup_check():
    """Ask the selected model for one tiny reply. A model that is missing or down is a result, not an error."""
    return jsonify(check_model()), 200, {'Cache-Control': 'no-store'}
