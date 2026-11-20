"""Private reflection controls and reports; no GET triggers inference."""
from flask import Blueprint, jsonify, request

from core.reflection import ReflectionError, get_reflection_service
from core.presence_schedule import ScheduleError

bp = Blueprint('reflection', __name__, url_prefix='/api/reflection')


@bp.after_request
def private(response):
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


@bp.app_errorhandler(ReflectionError)
@bp.app_errorhandler(ScheduleError)
def failure(error):
    return jsonify(error=str(error), code='reflection_error'), error.status, {'Cache-Control': 'no-store'}


def _session(value):
    if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 19:
        value = int(value)
    if type(value) is not int or not 0 < value < 2 ** 63:
        raise ReflectionError('session_id must be a positive integer.', 400)
    return value


def _body(allowed):
    from core.request_json import companion_body
    return companion_body(ReflectionError, 'Reflection', allowed)


def _query_session():
    if set(request.args) != {'session_id'} or len(request.args.getlist('session_id')) != 1:
        raise ReflectionError('Only one session_id query argument is supported.', 400)
    return _session(request.args.get('session_id'))


@bp.get('/preferences')
def preferences():
    return jsonify(get_reflection_service().preferences(_query_session()))


@bp.patch('/preferences')
def save_preferences():
    data = _body({'revision', 'settings'})
    return jsonify(get_reflection_service().update_preferences(_session(data.get('session_id')), data.get('dataset_epoch'),
                   data.get('revision'), data.get('settings'), avatar_id=data['avatar_id']))


@bp.post('/run')
def run():
    data = _body({'request_id'})
    return jsonify(job=get_reflection_service().run_now(_session(data.get('session_id')), data.get('dataset_epoch'), data.get('request_id'), avatar_id=data['avatar_id'])), 202


@bp.get('/reports')
def reports():
    return jsonify(get_reflection_service().reports(_query_session()))


@bp.patch('/notebook/<identifier>')
def edit_notebook(identifier):
    data = _body({'revision', 'text', 'uncertainty', 'dismiss'})
    changes = {key: value for key, value in data.items() if key not in {'session_id', 'dataset_epoch', 'avatar_id', 'revision'}}
    return jsonify(entry=get_reflection_service().edit_notebook(identifier, _session(data.get('session_id')), data.get('dataset_epoch'), data.get('revision'), changes, avatar_id=data['avatar_id']))
