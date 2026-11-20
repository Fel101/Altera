"""Bounded companion episode inspection and explicit curation controls."""
import json

from flask import Blueprint, jsonify, request

from core.episodes import EpisodeError, get_episode_service

bp = Blueprint('episodes', __name__, url_prefix='/api/episodes')


@bp.after_request
def private(response):
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


@bp.app_errorhandler(EpisodeError)
def failure(error):
    return jsonify(error=str(error), code='episode_error'), error.status, {'Cache-Control': 'no-store'}


def session_arg(value):
    if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 19:
        value = int(value)
    if type(value) is not int or not 0 < value < 2 ** 63:
        raise EpisodeError('session_id must be a positive integer.')
    return value


def body(allowed):
    if request.content_length is not None and request.content_length > 16384:
        raise EpisodeError('Episode request exceeds 16 KiB.', 413)
    raw = request.get_data(cache=True)
    if len(raw) > 16384:
        raise EpisodeError('Episode request exceeds 16 KiB.', 413)
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate JSON field.')
            result[key] = value
        return result
    def nonfinite(value):
        raise ValueError('Nonfinite JSON number.')
    try:
        data = json.loads(raw, object_pairs_hook=unique, parse_constant=nonfinite) if request.is_json else None
    except (ValueError, TypeError, UnicodeDecodeError):
        data = None
    if not isinstance(data, dict) or not set(data) <= set(allowed) | {'session_id', 'dataset_epoch', 'avatar_id'}:
        raise EpisodeError('Expected a JSON object with supported episode fields.')
    if not isinstance(data.get('avatar_id'), str) or len(data['avatar_id']) > 128:
        raise EpisodeError('The reviewed avatar_id is required, including an empty string for the default companion.')
    return data


@bp.get('')
def episodes():
    if not set(request.args) <= {'session_id'} or any(len(request.args.getlist(k)) != 1 for k in request.args):
        raise EpisodeError('Only one session_id query argument is supported.')
    return jsonify(get_episode_service().inventory(session_arg(request.args.get('session_id'))))


@bp.post('')
def capture():
    data = body({'request_id', 'kind', 'title', 'summary', 'uncertainty', 'sources', 'salience', 'in_story'})
    session_id, epoch = session_arg(data.pop('session_id', None)), data.pop('dataset_epoch', None)
    owner = data.pop('avatar_id')
    return jsonify(get_episode_service().capture(session_id, epoch, data, owner=owner)), 201


@bp.get('/sources')
def source_choices():
    if not set(request.args) <= {'session_id'} or any(len(request.args.getlist(k)) != 1 for k in request.args):
        raise EpisodeError('Only one session_id query argument is supported.')
    return jsonify(get_episode_service().sources(session_arg(request.args.get('session_id'))))


@bp.patch('/<episode_id>')
def correct(episode_id):
    data = body({'revision', 'title', 'summary', 'uncertainty', 'kind', 'salience', 'pinned', 'in_story'})
    session_id, epoch = session_arg(data.pop('session_id', None)), data.pop('dataset_epoch', None)
    revision = data.pop('revision', None)
    owner = data.pop('avatar_id')
    return jsonify(get_episode_service().change(episode_id, session_id, epoch, revision, data, owner=owner))


@bp.delete('/<episode_id>')
def forget(episode_id):
    data = body({'revision', 'confirm'})
    if data.get('confirm') != 'forget':
        raise EpisodeError('Confirm forgetting this episode.')
    return jsonify(get_episode_service().forget(episode_id, session_arg(data.get('session_id')),
                   data.get('dataset_epoch'), data.get('revision'), owner=data['avatar_id']))


@bp.get('/recall')
def recall():
    if not set(request.args) <= {'session_id', 'query'} or any(len(request.args.getlist(k)) != 1 for k in request.args):
        raise EpisodeError('Only session_id and query arguments are supported.')
    return jsonify(get_episode_service().preview_recall(session_arg(request.args.get('session_id')), request.args.get('query', '')))


@bp.get('/<episode_id>/restore-review')
def restore_review(episode_id):
    if not set(request.args) <= {'session_id', 'dataset_epoch', 'avatar_id', 'revision'} or any(len(request.args.getlist(k)) != 1 for k in request.args):
        raise EpisodeError('Unsupported restored-episode review arguments.')
    raw_revision = request.args.get('revision', '')
    if not raw_revision.isascii() or not raw_revision.isdecimal() or len(raw_revision) > 10:
        raise EpisodeError('A nonnegative revision is required.')
    owner = request.args.get('avatar_id')
    if not isinstance(owner, str) or len(owner) > 128:
        raise EpisodeError('The reviewed avatar_id is required, including an empty string for the default companion.')
    return jsonify(get_episode_service().review_restored(episode_id, session_arg(request.args.get('session_id')),
        request.args.get('dataset_epoch'), int(raw_revision), owner=owner))


@bp.post('/<episode_id>/revalidate')
def revalidate(episode_id):
    data = body({'revision', 'reviewed_sources', 'confirm'})
    if data.get('confirm') != 'restore-curation':
        raise EpisodeError('Confirm restoring the reviewed saved curation.')
    return jsonify(get_episode_service().revalidate_restored(episode_id, session_arg(data.get('session_id')),
        data.get('dataset_epoch'), data.get('revision'), data.get('reviewed_sources'), owner=data['avatar_id']))
