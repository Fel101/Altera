"""Private durable voice-note drafts; review precedes the separate chat send."""
from flask import Blueprint, jsonify, request, send_file

from core.voice_notes import VoiceNoteError, get_voice_note_service

bp = Blueprint('voice_notes', __name__, url_prefix='/api/voice-notes')


@bp.after_request
def private_audio(response):
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


@bp.app_errorhandler(VoiceNoteError)
def voice_note_error(error):
    return jsonify(error=str(error), code='voice_note_error'), error.status, {'Cache-Control': 'no-store'}


def body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise VoiceNoteError('Expected a JSON object.', 400)
    return data






def session_arg(value):
    if isinstance(value, bool):
        raise VoiceNoteError('session_id must be a positive integer.', 400)
    try:
        converted = int(value)
        if str(converted) != str(value) or converted <= 0:
            raise ValueError()
        return converted
    except (TypeError, ValueError):
        raise VoiceNoteError('session_id must be a positive integer.', 400) from None


def nonnegative_arg(value, field):
    try:
        converted = int(value)
        if isinstance(value, bool) or str(converted) != str(value) or converted < 0:
            raise ValueError()
        return converted
    except (TypeError, ValueError):
        raise VoiceNoteError(field + ' must be a nonnegative integer from the recording context.', 400) from None


@bp.get('/context')
def recording_context():
    from core.datastore import get_datastore
    from core.voice_notes import recording_context as read_context
    if set(request.args) != {'session_id'} or len(request.args.getlist('session_id')) != 1:
        raise VoiceNoteError('Select exactly one conversation for review.', 400)
    return jsonify(read_context(get_datastore(), session_arg(request.args.get('session_id'))))


@bp.get('')
def list_notes():
    return jsonify(notes=get_voice_note_service().list(session_arg(request.args.get('session_id'))))


@bp.post('')
def upload_note():
    if 'audio' not in request.files:
        raise VoiceNoteError('Attach a recording in the audio field.', 400)
    if 'avatar_id' not in request.form:
        raise VoiceNoteError('Capture the companion in the recording context before uploading.', 400)
    result = get_voice_note_service().upload(request.form.get('note_id'), session_arg(request.form.get('session_id')),
                                             request.form.get('dataset_epoch'), request.files['audio'].stream,
                                             source_revision=nonnegative_arg(request.form.get('source_revision'), 'source_revision'),
                                             recall_after=nonnegative_arg(request.form.get('recall_after'), 'recall_after'),
                                             avatar_id=request.form.get('avatar_id'))
    return jsonify(note=result), 201


@bp.get('/<note_id>')
def get_note(note_id):
    value = request.args.get('session_id')
    session_id = session_arg(value) if value is not None else None
    return jsonify(note=get_voice_note_service().get(note_id, session_id))


@bp.put('/<note_id>')
def edit_note(note_id):
    data = body()
    return jsonify(note=get_voice_note_service().edit(note_id, data.get('dataset_epoch'), data.get('transcript'), data.get('revision')))


@bp.post('/<note_id>/transcribe')
def transcribe_note(note_id):
    result = get_voice_note_service().transcribe(note_id, body().get('dataset_epoch'))
    return jsonify(note=result), 202 if result['status'] == 'transcribing' else 200


@bp.post('/<note_id>/review')
def review_note(note_id):
    data = body()
    return jsonify(note=get_voice_note_service().review(note_id, data.get('dataset_epoch'), data.get('revision')))


@bp.get('/<note_id>/audio')
def play_input(note_id):
    path, mimetype = get_voice_note_service().audio(note_id)
    return send_file(path, mimetype=mimetype, conditional=True, max_age=0)


@bp.get('/<note_id>/replies/<filename>')
def play_reply(note_id, filename):
    path, mimetype = get_voice_note_service().audio(note_id, filename)
    return send_file(path, mimetype=mimetype, conditional=True, max_age=0)






@bp.delete('/<note_id>')
def delete_note(note_id):
    data = body()
    if data.get('confirm') != 'delete-voice-note':
        raise VoiceNoteError('Confirm deleting the saved audio and transcript.', 400)
    service = get_voice_note_service()
    service._epoch(data.get('dataset_epoch'))
    service.delete(note_id)
    return jsonify(status='deleted')
