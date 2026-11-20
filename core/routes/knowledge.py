"""Explicit, companion-scoped memory controls and source inspection."""

from flask import Blueprint, Response, g, jsonify, request

from core.knowledge import KnowledgeError, can_recall, get_knowledge_service

bp = Blueprint('knowledge', __name__, url_prefix='/api/knowledge')


@bp.after_request
def private_knowledge(response):
    response.headers['Cache-Control'] = 'no-store'
    return response


@bp.app_errorhandler(KnowledgeError)
def knowledge_error(error):
    return jsonify(error=str(error), code='knowledge_error'), error.status


def owner_of(scope):
    if scope == '_default':
        return ''
    from core.companion import get_services
    if not get_services().avatars.get(scope):
        raise KnowledgeError('Companion not found.', 404)
    return scope


def body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise KnowledgeError('Expected a JSON object.')
    return data








def _report_snapshot(scope):
    from core.companion import get_services
    from core.knowledge_report import build_report
    from core.state_gate import STATE
    # Briefly refuse concurrent writers rather than mixing note/file/vector revisions.
    # The request's own read lease is allowed; no model or photo work is started.
    with STATE.exclusive(allow_lease=getattr(g, 'altera_state_lease', None)):
        owner = owner_of(scope)
        avatar = get_services().avatars.get(owner) if owner else None
        return build_report(get_knowledge_service(), owner, (avatar or {}).get('name') or 'Default companion')


@bp.get('/scopes/<scope>/report')
def knowledge_report(scope):
    return jsonify(_report_snapshot(scope))


@bp.get('/scopes/<scope>/report/export')
def export_knowledge_report(scope):
    from core.knowledge_report import render_markdown
    fmt = request.args.get('format', 'json')
    if fmt not in {'json', 'markdown'}:
        raise KnowledgeError('Report format must be json or markdown.')
    report = _report_snapshot(scope)
    if fmt == 'json':
        import json
        content = json.dumps(report, ensure_ascii=False, indent=2)
        mimetype, extension = 'application/json', 'json'
    else:
        content, mimetype, extension = render_markdown(report), 'text/markdown', 'md'
    # Scope names/IDs never enter a response header or filesystem path.
    return Response(content, mimetype=mimetype, headers={
        'Content-Disposition': f'attachment; filename="altera-companion-knowledge.{extension}"',
        'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})


@bp.get('/scopes')
def scopes():
    from core.companion import get_services
    avatars = get_services().avatars
    from core.datastore import get_datastore
    ds = get_datastore()
    return jsonify(scopes=[{'id': '_default', 'name': 'Default companion / legacy shared memories', 'session_id': ds.get_avatar_session_id('')}] +
                   [{'id': a['id'], 'name': a.get('name') or a['id'], 'session_id': ds.get_avatar_session_id(a['id'])} for a in avatars.list()],
                   active=avatars.active_id() or '_default')


@bp.patch('/scopes/<scope>/memories/<memory_id>/priority')
def memory_priority(scope, memory_id):
    data = body()
    if not set(data) <= {'dataset_epoch', 'revision', 'content_sha256', 'pinned', 'salience'}:
        raise KnowledgeError('Unsupported memory priority field.')
    changes = {key: data[key] for key in ('pinned', 'salience') if key in data}
    return jsonify(get_knowledge_service().prioritize(owner_of(scope), memory_id, epoch=data.get('dataset_epoch'),
                   revision=data.get('revision'), content_sha256=data.get('content_sha256'), changes=changes))


@bp.get('/messages/<int:message_id>')
def message_source(message_id):
    from core.datastore import get_datastore
    from core.companion import get_services
    store = get_datastore()
    message = store.get_message(message_id)
    session = store.get_session(message.session_id) if message else None
    if not session:
        raise KnowledgeError('Message not found.', 404)
    avatar = get_services().avatars.get(session.avatar_id) if session.avatar_id else None
    from core.knowledge import floor, latest_message_id, reference
    return jsonify(message=message.__dict__, scope=session.avatar_id or '_default',
                   dataset_epoch=store.dataset_epoch()['value'], review_through=latest_message_id(store, session.avatar_id or ''),
                   recall_after=floor(store, session.avatar_id or ''), source_sha256=reference(message)['sha256'],
                   name=(avatar or {}).get('name') or 'Default companion'), 200, {'Cache-Control': 'no-store'}


@bp.route('/scopes/<scope>/memories', methods=['GET', 'POST', 'DELETE'])
def memories(scope):
    owner, service = owner_of(scope), get_knowledge_service()
    if request.method == 'GET':
        return jsonify(service.inventory(owner)), 200, {'Cache-Control': 'no-store'}
    data = body()
    if request.method == 'DELETE':
        if data.get('confirm') != 'forget-all':
            raise KnowledgeError('Confirm forgetting this companion’s memories.')
        return jsonify(service.clear(owner))
    from core.routes.core_api import _validate_memory
    error = _validate_memory(data, require_content=True)
    if error:
        raise KnowledgeError(error)
    from core import safety
    if safety.blocks_message(data['content']):
        raise KnowledgeError('That content cannot be saved as a memory.', 422)
    memory_id = service.remember(owner, data['content'].strip(), data.get('type', 'fact'), float(data.get('confidence', 1.)),
                                 source_id=data.get('source_message_id'), session_id=data.get('session_id'))
    return jsonify(id=memory_id, status='ok'), 201


@bp.route('/scopes/<scope>/memories/<memory_id>', methods=['PUT', 'DELETE'])
def change_memory(scope, memory_id):
    owner, service = owner_of(scope), get_knowledge_service()
    if request.method == 'DELETE':
        if (body()).get('confirm') != 'forget':
            raise KnowledgeError('Confirm forgetting this memory and clearing derived notes.')
        return jsonify(service.change(owner, memory_id, forget=True))
    data = body()
    from core.routes.core_api import _validate_memory
    error = _validate_memory(data, require_content=True)
    if error:
        raise KnowledgeError(error)
    from core import safety
    if safety.blocks_message(data['content']):
        raise KnowledgeError('That content cannot be saved as a memory.', 422)
    return jsonify(service.change(owner, memory_id, content=data['content'].strip(),
                                  confidence=float(data['confidence']) if 'confidence' in data else None))


@bp.post('/scopes/<scope>/repair')
def repair(scope):
    if body().get('confirm') != 'rebuild':
        raise KnowledgeError('Confirm rebuilding automatic memories and clearing stale notes.')
    return jsonify(get_knowledge_service().repair(owner_of(scope)))


@bp.route('/scopes/<scope>/duplicates', methods=['GET', 'POST'])
def duplicates(scope):
    owner, service = owner_of(scope), get_knowledge_service()
    groups = service.manager.deduplicator.find_duplicates([m for m in service._memories(owner) if can_recall(m, service.ds)])
    if request.method == 'GET':
        return jsonify(groups=groups)
    if body().get('confirm') != 'merge':
        raise KnowledgeError('Confirm merging the reviewed duplicate memories.')
    with service.changing(owner):
        groups = service.manager.deduplicator.find_duplicates([m for m in service._memories(owner) if can_recall(m, service.ds)])
        result = service.manager.deduplicator.consolidate_duplicates(groups)
    return jsonify(status='ok', merged=len(result))
