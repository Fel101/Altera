"""Private companion inbox; reads never generate or automatically accept text."""
from flask import Blueprint, jsonify, request

from core.presence import PresenceError, get_presence_service
from core.presence_schedule import ScheduleError

bp = Blueprint('presence', __name__, url_prefix='/api/presence')


@bp.after_request
def private(response):
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


@bp.app_errorhandler(PresenceError)
@bp.app_errorhandler(ScheduleError)
def failure(error):
    return jsonify(error=str(error), code='presence_error'), error.status, {'Cache-Control': 'no-store'}


def body(allowed):
    from core.request_json import companion_body
    return companion_body(PresenceError, 'Presence', allowed)


def session_arg(value):
    if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 19:
        value = int(value)
    if type(value) is not int or not 0 < value < 2 ** 63:
        raise PresenceError('session_id must be a positive integer.', 400)
    return value


def query_session():
    if set(request.args) != {'session_id'} or len(request.args.getlist('session_id')) != 1:
        raise PresenceError('Only one session_id query argument is supported.', 400)
    return session_arg(request.args.get('session_id'))


@bp.get('/preferences')
def preferences():
    return jsonify(get_presence_service().preferences(query_session()))


@bp.patch('/preferences')
def save_preferences():
    data = body({'revision', 'settings'})
    return jsonify(get_presence_service().update_preferences(session_arg(data.get('session_id')),
                   data.get('dataset_epoch'), data.get('revision'), data.get('settings'), avatar_id=data['avatar_id']))


@bp.get('/inbox')
def inbox():
    return jsonify(get_presence_service().inbox(query_session()))


@bp.get('/notices')
def notices():
    """New unread check-ins across everyone, for the browser's notifications: ?after=<seconds>&text=1 (text only on request)."""
    if not set(request.args) <= {'after', 'text'} or any(len(request.args.getlist(k)) > 1 for k in request.args):
        raise PresenceError('Only after and text are supported.', 400)
    after = request.args.get('after')
    try:
        after = float(after) if after else None
    except ValueError:
        raise PresenceError('after must be a number of seconds.', 400) from None
    if after is not None and not after >= 0:
        raise PresenceError('after must be a number of seconds.', 400)
    return jsonify(get_presence_service().notices(after, include_text=request.args.get('text') == '1'))


@bp.post('/inbox/<item_id>/<action>')
def inbox_action(item_id, action):
    data = body(set())
    return jsonify(item=get_presence_service().action(item_id, action, session_arg(data.get('session_id')),
                   data.get('dataset_epoch'), avatar_id=data['avatar_id']))


@bp.post('/check-now')
def check_now():
    data = body({'request_id'})
    return jsonify(job=get_presence_service().check_now(session_arg(data.get('session_id')), data.get('dataset_epoch'),
                   data.get('request_id'), avatar_id=data['avatar_id'])), 202


@bp.get('/rituals')
def rituals():
    return jsonify(rituals=get_presence_service().rituals(query_session()))


@bp.post('/rituals')
def add_ritual():
    data = body({'kind', 'minute', 'weekday', 'enabled'})
    settings = {k: v for k, v in data.items() if k not in {'session_id', 'dataset_epoch', 'avatar_id'}}
    return jsonify(ritual=get_presence_service().ritual(session_arg(data.get('session_id')), data.get('dataset_epoch'), settings, avatar_id=data['avatar_id'])), 201


@bp.route('/rituals/<ritual_id>', methods=['PUT', 'DELETE'])
def change_ritual(ritual_id):
    data = body({'kind', 'minute', 'weekday', 'enabled', 'revision'})
    settings = {k: v for k, v in data.items() if k not in {'session_id', 'dataset_epoch', 'avatar_id', 'revision'}}
    return jsonify(ritual=get_presence_service().ritual(session_arg(data.get('session_id')), data.get('dataset_epoch'),
                   settings, ritual_id=ritual_id, revision=data.get('revision'), delete=request.method == 'DELETE', avatar_id=data['avatar_id']))
