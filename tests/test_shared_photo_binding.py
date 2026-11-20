"""Atomic shared-picture chat binding, using only temporary SQLite data."""
from concurrent.futures import ThreadPoolExecutor
import base64
import json
import threading
from types import SimpleNamespace

import pytest

from core.datastore import DataStore
from core import companion, datastore, imagegen, state_gate
from core.photos import PhotoService
from test_blueprints import create_app
from test_shared_photos import Avatars, Backend, Checker, create
from test_shared_reference import png


@pytest.fixture
def world(tmp_path):
    checker, backend = Checker(), Backend()
    service = PhotoService(tmp_path / 'photos', Avatars(), lambda level: backend, checker, sync=True,
                           config={'imagegen': {'unload_llm_before_generate': False}})
    return service, checker, backend


@pytest.fixture
def binding(tmp_path):
    store = DataStore(str(tmp_path / 'chat.db'))
    sid = store.create_session('Synthetic chat')
    store.set_session_avatar(sid, 'companion')
    epoch = store.dataset_epoch()['value']
    return store, sid, epoch


def test_shared_photo_binding_is_atomic_idempotent_and_resists_changed_context(binding):
    store, sid, epoch = binding
    request, photo = 'shared_' + 'a' * 32, 'p-1234567890'
    def save():
        return store.record_shared_photo(sid, 'companion', epoch, request, photo)
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(lambda _: save(), range(8)))
    assert len(set(ids)) == 1
    assert [row.content for row in store.get_messages(sid)] == [f'[[photo:{photo}]]']
    for args in ((sid, 'other', epoch, request, photo), (sid, 'companion', 'old-epoch', request, photo),
                 (sid, 'companion', epoch, request, 'p-abcdefghij')):
        with pytest.raises(ValueError):
            store.record_shared_photo(*args)
    with store._connect() as conn:
        conn.execute('DELETE FROM messages WHERE id=?', (ids[0],))
        conn.commit()
    with pytest.raises(ValueError, match='changed or removed'):
        save()
    assert not store.get_messages(sid)


def test_shared_photo_callback_binds_before_generation_and_retry_does_not_duplicate(binding, world):
    store, sid, epoch = binding
    service, checker, backend = world
    request = 'shared_' + 'a' * 32
    def attach(pid):
        assert not checker.calls and not backend.calls
        return store.record_shared_photo(sid, 'companion', epoch, request, pid)
    first = create(service, session_id=sid, dataset_epoch=epoch, request_id=request, on_accept=attach)
    assert first['status'] == 'ready'
    # The retry invokes the same binding but must never run image generation again.
    def reattach(pid):
        return store.record_shared_photo(sid, 'companion', epoch, request, pid)
    assert create(service, session_id=sid, dataset_epoch=epoch, request_id=request, on_accept=reattach)['id'] == first['id']
    assert len(store.get_messages(sid)) == 1 and len(backend.calls) == 1


def test_failed_context_binding_preserves_failed_record_without_provider_calls(binding, world):
    store, sid, epoch = binding
    service, checker, backend = world
    request = 'shared_' + 'a' * 32
    def attach(pid):
        return store.record_shared_photo(sid, 'companion', 'stale-epoch', request, pid)
    with pytest.raises(ValueError, match='conversation or companion changed'):
        create(service, session_id=sid, dataset_epoch=epoch, request_id=request, on_accept=attach)
    assert not checker.calls and not backend.calls and not store.get_messages(sid)
    assert service._all()[0]['status'] == 'failed'


def test_session_deletion_waits_for_owned_picture_work(binding, monkeypatch):
    store, sid, _ = binding
    gate = state_gate.StateGate()
    monkeypatch.setattr(state_gate, 'STATE', gate)
    monkeypatch.setattr(datastore, '_datastore_instance', store)
    app = create_app('sessions', 'backups')
    busy = gate.enter('altera-photo')
    try:
        result = app.test_client().delete(f'/api/sessions/{sid}')
        assert result.status_code == 409 and store.get_session(sid) is not None
    finally:
        busy.release()
    result = app.test_client().delete(f'/api/sessions/{sid}')
    assert result.status_code == 200 and store.get_session(sid) is None


def test_shared_photo_cannot_be_deleted_while_its_worker_can_still_publish(tmp_path, monkeypatch):
    gate = state_gate.StateGate()
    monkeypatch.setattr(state_gate, 'STATE', gate)
    checker, backend = Checker(), Backend()
    service = PhotoService(tmp_path / 'photos', Avatars(), lambda level: backend, checker,
                           config={'imagegen': {'unload_llm_before_generate': False}})
    monkeypatch.setattr(companion, 'get_services', lambda: SimpleNamespace(photos=service))
    entered, release = threading.Event(), threading.Event()
    original = service._process_shared
    def pending(pid, data):
        entered.set()
        assert release.wait(5)
        original(pid, data)
    monkeypatch.setattr(service, '_process_shared', pending)
    app = create_app('companion', 'backups')
    try:
        result = create(service)
        assert entered.wait(3)
        response = app.test_client().delete('/api/photos/' + result['id'])
        assert response.status_code == 409 and service.get(result['id']) is not None
    finally:
        release.set()
    assert gate.join_jobs(timeout=5)
    assert app.test_client().delete('/api/photos/' + result['id']).status_code == 200
    assert service.get(result['id']) is None and not service._png(result['id']).exists()


@pytest.fixture
def shared_api(binding, world, monkeypatch):
    store, sid, epoch = binding
    service, checker, backend = world
    gate = state_gate.StateGate()
    monkeypatch.setattr(state_gate, 'STATE', gate)
    monkeypatch.setattr(datastore, '_datastore_instance', store)
    class TargetChecker(Checker):
        target = ('http://127.0.0.1:9001/v1', 'synthetic-key', 'fixture-model')
        def _target(self):
            return self.target
        def check_subjects(self, image, count, *, target=None):
            self.last_target = target
            return super().check_subjects(image, count)
        def check(self, image):
            return self.check_subjects(image, 1)
    recipients = []
    class TargetBackend(imagegen.MageFlowBackend):
        def __init__(self, comfy, delegate):
            self.comfy, self.delegate = comfy, delegate
        def __deepcopy__(self, memo):
            return TargetBackend(SimpleNamespace(base=self.comfy.base), self.delegate)
        def render(self, *args):
            recipients.append(self.comfy.base)
            return self.delegate.render(*args)
    checker = TargetChecker()
    service.checker = checker
    comfy = SimpleNamespace(base='http://127.0.0.1:9002')
    service.backend_for = lambda level: TargetBackend(comfy, backend)
    services = SimpleNamespace(photos=service, avatars=service.avatars, checker=checker, comfy=comfy,
                               imagegen_config={'enabled': True})
    monkeypatch.setattr(companion, 'get_services', lambda: services)
    client = create_app('companion', 'backups').test_client()
    data = {'session_id': sid, 'avatar_id': 'companion', 'dataset_epoch': epoch, 'request_id': 'shared_' + 'a' * 32,
            'scene': 'walking in a garden', 'reference_png': base64.b64encode(png(metadata=b'private metadata')).decode(),
            'self_reference': True, 'adult_confirmation': True, 'provider_confirmation': True}
    review = client.post('/api/shared-scenes/context', json={key: data[key] for key in ('session_id', 'avatar_id', 'dataset_epoch')})
    assert review.status_code == 200
    data['provider_fingerprint'] = review.json['provider_fingerprint']
    return SimpleNamespace(client=client, data=data, store=store, service=service, checker=checker,
                           backend=backend, comfy=comfy, review=review, recipients=recipients)


def test_shared_route_binds_one_reviewed_request_and_cannot_resurrect_deleted_image(shared_api):
    w = shared_api
    response = w.client.post('/api/shared-scenes', json=w.data)
    assert response.status_code == 202 and response.json['status'] == 'ready'
    pid, mid = response.json['id'], response.json['message_id']
    assert w.store.get_message(mid).content == f'[[photo:{pid}]]'
    retry = w.client.post('/api/shared-scenes', json=w.data)
    assert retry.status_code == 202 and retry.json['id'] == pid and retry.json['message_id'] == mid
    assert len(w.store.get_messages(w.data['session_id'])) == 1 and len(w.backend.calls) == 1
    assert w.client.delete('/api/photos/' + pid).status_code == 200
    before = set(w.service.root.iterdir())
    refused = w.client.post('/api/shared-scenes', json=w.data)
    assert refused.status_code == 409 and set(w.service.root.iterdir()) == before and len(w.backend.calls) == 1


def test_shared_picture_stays_imagined_in_chat_and_notebook_context(shared_api):
    from core.chat import ChatEngine
    from core.notes import transcript

    w = shared_api
    response = w.client.post('/api/shared-scenes', json=w.data)
    assert response.status_code == 202 and response.json['status'] == 'ready'
    assert response.json['provenance'] == 'imagined_scene'
    marker = w.store.get_message(response.json['message_id'])
    history = ChatEngine._history_text(None, marker.content)
    assert 'imagined image was sent' in history and 'not evidence of a real event' in history
    assert 'Requested scene: walking in a garden.' in history and '[[photo:' not in history
    notebook = transcript([marker], 'Companion')
    assert 'imagined image was requested' in notebook and 'not evidence of a real event' in notebook
    assert '[[photo:' not in notebook and 'walking in a garden' not in notebook
    with w.store._connect() as conn:
        assert conn.execute('SELECT count(*) FROM companion_episodes').fetchone()[0] == 0

    assert w.client.delete('/api/photos/' + response.json['id']).status_code == 200
    unavailable = ChatEngine._history_text(None, marker.content)
    assert 'status is unavailable' in unavailable and 'not evidence of a real event' in unavailable
    assert 'imagined image was sent' not in unavailable


@pytest.mark.parametrize('change', [
    {'self_reference': False}, {'adult_confirmation': False}, {'provider_confirmation': False},
    {'reference_png': 'invalid base64!'}, {'avatar_id': 'other'}, {'dataset_epoch': 'old'},
    {'session_id': True}, {'request_id': 'bad'}, {'extra': True},
])
def test_shared_route_refuses_unreviewed_or_stale_context_before_provider(shared_api, change):
    w = shared_api
    response = w.client.post('/api/shared-scenes', json={**w.data, **change})
    assert response.status_code in (400, 409)
    assert not w.checker.calls and not w.backend.calls and not list(w.service.root.iterdir())


def test_shared_route_refuses_cross_site_before_provider(shared_api):
    w = shared_api
    response = w.client.post('/api/shared-scenes', json=w.data, headers={'Sec-Fetch-Site': 'cross-site'})
    assert response.status_code == 403 and not w.checker.calls and not w.backend.calls


def test_enabling_kids_mode_disables_picture_requests_even_after_recipient_review(shared_api, monkeypatch):
    w = shared_api
    avatar = w.service.avatars.get('companion')
    monkeypatch.setattr(w.service.avatars, 'get', lambda avatar_id: {**avatar, 'kind': 'pal', 'kids': True})
    scope = {key: w.data[key] for key in ('session_id', 'avatar_id', 'dataset_epoch')}

    review = w.client.post('/api/shared-scenes/context', json=scope)
    shared = w.client.post('/api/shared-scenes', json=w.data)
    camera = w.client.post('/api/photos', json={'session_id': scope['session_id'], 'scene': w.data['scene']})

    for result in (review, shared, camera):
        assert result.status_code == 422 and 'kids mode' in result.json['error'].lower()
    assert not w.checker.calls and not w.backend.calls and not list(w.service.root.iterdir())
    assert not w.store.get_messages(scope['session_id'])


def test_queued_shared_picture_checks_kids_mode_before_sending_reference(shared_api, monkeypatch):
    w = shared_api
    w.service.sync = False
    entered, release = threading.Event(), threading.Event()
    process = w.service._process_shared

    def wait_for_settings(pid, reference):
        entered.set()
        assert release.wait(5)
        process(pid, reference)

    monkeypatch.setattr(w.service, '_process_shared', wait_for_settings)
    try:
        result = w.client.post('/api/shared-scenes', json=w.data)
        assert result.status_code == 202 and entered.wait(5)
        avatar = w.service.avatars.get('companion')
        monkeypatch.setattr(w.service.avatars, 'get', lambda avatar_id: {**avatar, 'kind': 'pal', 'kids': True})
    finally:
        release.set()
    assert state_gate.STATE.join_jobs(timeout=5)
    saved = w.service.get(result.json['id'])
    assert saved['status'] == 'blocked' and 'kids mode' in saved['reason']
    assert not w.checker.calls and not w.backend.calls


@pytest.mark.parametrize('changed', ['vision_url', 'vision_key', 'vision_model', 'image_url'])
def test_changed_recipient_requires_fresh_approval_without_provider_calls(shared_api, changed):
    w = shared_api
    original = w.checker.target
    if changed == 'vision_url':
        w.checker.target = ('http://127.0.0.1:9003/v1', *original[1:])
    elif changed == 'vision_key':
        w.checker.target = (original[0], 'different-key', original[2])
    elif changed == 'vision_model':
        w.checker.target = (*original[:2], 'different-model')
    else:
        w.comfy.base = 'http://127.0.0.1:9004'
    result = w.client.post('/api/shared-scenes', json=w.data)
    assert result.status_code == 409 and result.json['code'] == 'shared_providers_changed'
    assert not w.checker.calls and not w.backend.calls and not list(w.service.root.iterdir())
    assert b'synthetic-key' not in w.review.data


def test_shared_context_review_has_no_provider_calls_and_rejects_stale_scope(shared_api):
    w = shared_api
    assert w.review.json['vision_endpoint'] == 'http://127.0.0.1:9001/v1'
    assert w.review.json['image_endpoint'] == 'http://127.0.0.1:9002'
    result = w.client.post('/api/shared-scenes/context', json={'session_id': w.data['session_id'],
        'avatar_id': 'other', 'dataset_epoch': w.data['dataset_epoch']})
    assert result.status_code == 409 and not w.checker.calls and not w.backend.calls


@pytest.mark.parametrize(('uses_second_reference', 'allow_fallback', 'selected'), [
    (False, False, None),
    (False, True, 'builtin'),
    (True, True, 'workflow'),
])
def test_shared_workflow_requires_both_inputs_before_sending(
        shared_api, tmp_path, monkeypatch, uses_second_reference, allow_fallback, selected):
    w = shared_api
    builtin = w.service.backend_for('off')
    inputs = {'image': '{{reference}}'}
    if uses_second_reference:
        inputs['second_image'] = '{{reference_2}}'
    path = tmp_path / 'workflow.json'
    path.write_text(json.dumps({'1': {'inputs': inputs, '_meta': {'title': '{{reference_2}}'}}}), encoding='utf-8')
    workflow = imagegen.WorkflowBackend(w.comfy, path)
    workflow_calls = []

    def render(_self, refs, *args):
        workflow_calls.append(len(refs))
        return w.backend.render(refs, *args)

    monkeypatch.setattr(imagegen.WorkflowBackend, 'render', render)
    backend = imagegen.FallbackBackend(workflow, builtin) if allow_fallback else workflow
    w.service.backend_for = lambda level: backend
    scope = {key: w.data[key] for key in ('session_id', 'avatar_id', 'dataset_epoch')}
    review = w.client.post('/api/shared-scenes/context', json=scope)
    assert not w.checker.calls and not w.backend.calls

    if selected is None:
        assert review.status_code == 409 and 'both image reference placeholders' in review.json['error']
        submit = w.client.post('/api/shared-scenes', json=w.data)
        assert submit.status_code == 409
        assert not w.checker.calls and not workflow_calls and not w.backend.calls
        assert not w.store.get_messages(scope['session_id']) and not list(w.service.root.iterdir())
        return

    assert review.status_code == 200
    submit = w.client.post('/api/shared-scenes', json={**w.data, 'provider_fingerprint': review.json['provider_fingerprint']})
    assert submit.status_code == 202 and submit.json['provenance'] == 'imagined_scene'
    assert submit.json['status'] == 'ready' and [count for _, count in w.checker.calls] == [1, 2]
    if selected == 'builtin':
        assert w.recipients == [w.comfy.base] and not workflow_calls
    else:
        assert workflow_calls == [2] and not w.recipients


def test_queued_job_keeps_approved_recipients_after_settings_change(shared_api, monkeypatch):
    w = shared_api
    w.service.sync = False
    entered, release = threading.Event(), threading.Event()
    process = w.service._process_shared
    def held(pid, reference):
        entered.set()
        assert release.wait(5)
        process(pid, reference)
    monkeypatch.setattr(w.service, '_process_shared', held)
    try:
        result = w.client.post('/api/shared-scenes', json=w.data)
        assert result.status_code == 202 and entered.wait(3)
        w.checker.target = ('http://127.0.0.1:9003/v1', 'new-key', 'new-model')
        w.comfy.base = 'http://127.0.0.1:9004'
    finally:
        release.set()
    assert state_gate.STATE.join_jobs(timeout=5)
    assert w.checker.last_target == ('http://127.0.0.1:9001/v1', 'synthetic-key', 'fixture-model')
    assert w.recipients == ['http://127.0.0.1:9002']
    assert w.service.get(result.json['id'])['status'] == 'ready'
