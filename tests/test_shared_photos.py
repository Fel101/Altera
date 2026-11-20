"""Shared-scene service checks with synthetic images and fake providers."""
from collections import deque
import json
import threading
from types import SimpleNamespace

import pytest

from core import photos, state_gate
from core.photos import PhotoBlocked, PhotoError, PhotoService
from test_shared_reference import png


class Avatars:
    def get(self, avatar_id):
        return {'id': avatar_id, 'card': {'age': 30, 'gender': 'female'}} if avatar_id == 'companion' else None

    def image_path(self, avatar_id, kind):
        return 'synthetic-portrait' if avatar_id == 'companion' and kind == 'portrait' else None

    def refs(self, avatar_id, body):
        return [png()] if avatar_id == 'companion' and not body else []


class Checker:
    def __init__(self, results=(True, True)):
        self.results, self.calls = deque(results), []

    def check_subjects(self, image, count):
        self.calls.append((image, count))
        return {'ok': self.results.popleft()}


class Backend:
    def __init__(self, image=None):
        self.calls, self.image = [], image if image is not None else png(metadata=b'generated private metadata')

    def render(self, refs, prompt, negative, seed, size, steps):
        self.calls.append((refs, prompt, negative, seed, size, steps))
        return self.image


@pytest.fixture
def world(tmp_path):
    checker, backend = Checker(), Backend()
    service = PhotoService(tmp_path / 'photos', Avatars(), lambda level: backend, checker, sync=True,
                           config={'imagegen': {'unload_llm_before_generate': False}})
    return service, checker, backend


def create(service, **changes):
    values = {'scene': 'walking in a garden', 'reference_png': png(metadata=b'private reference metadata'),
              'avatar_id': 'companion', 'session_id': 1, 'dataset_epoch': 'epoch',
              'request_id': 'shared_' + 'a' * 32, 'self_attested': True}
    values.update(changes)
    return service.create_shared(**values)


def test_shared_scene_uses_two_distinct_refs_and_keeps_original_out_of_storage(world):
    service, checker, backend = world
    result = create(service)
    assert result['status'] == 'ready' and result['provenance'] == 'imagined_scene' and result['age_checked']
    assert [count for _, count in checker.calls] == [1, 2]
    refs, prompt, negative, *_ = backend.calls[0]
    assert len(refs) == 2 and b'private reference metadata' not in refs[1]
    assert 'first is the companion' in prompt and 'second is the user' in prompt and 'extra people' in negative
    assert b'generated private metadata' not in service.image_path(result['id']).read_bytes()
    for path in service.root.iterdir():
        assert b'private reference metadata' not in path.read_bytes()
    assert result['id'] not in [row['id'] for row in service.gallery('companion', 'off')]
    with pytest.raises(PhotoError, match='reference again'):
        service.reroll(result['id'])


def test_shared_retry_is_idempotent_and_conflicting_identity_is_refused(world):
    service, _, backend = world
    first = create(service)
    assert create(service)['id'] == first['id'] and len(backend.calls) == 1
    with pytest.raises(PhotoError, match='different shared image'):
        create(service, scene='sitting by a lake')
    assert len(backend.calls) == 1


def test_picture_record_replacement_retries_brief_sharing_conflicts(world, monkeypatch):
    service, _, _ = world
    record = {'id': 'p-0123456789', 'status': 'pending'}
    service._save(record)
    replace = photos.os.replace
    attempts, waits = [], []
    def intermittent(source, target):
        attempts.append((source, target))
        if len(attempts) < 3:
            raise PermissionError('synthetic sharing conflict')
        return replace(source, target)
    monkeypatch.setattr(photos, '_retryable_replace_error', lambda error: True)
    monkeypatch.setattr(photos.os, 'replace', intermittent)
    monkeypatch.setattr(photos.time, 'sleep', waits.append)
    service._save({**record, 'status': 'ready'})
    assert len(attempts) == 3 and waits == [0.02, 0.05]
    assert json.loads(service._json(record['id']).read_text('utf-8'))['status'] == 'ready'


def test_persistent_picture_record_denial_preserves_original(world, monkeypatch):
    service, _, _ = world
    record = {'id': 'p-0123456789', 'status': 'pending'}
    service._save(record)
    path = service._json(record['id'])
    original, attempts, waits = path.read_bytes(), [], []
    def denied(source, target):
        attempts.append((source, target))
        raise PermissionError('synthetic persistent denial')
    monkeypatch.setattr(photos, '_retryable_replace_error', lambda error: True)
    monkeypatch.setattr(photos.os, 'replace', denied)
    monkeypatch.setattr(photos.time, 'sleep', waits.append)
    with pytest.raises(PermissionError):
        service._save({**record, 'status': 'ready'})
    assert len(attempts) == 5 and waits == [0.02, 0.05, 0.1, 0.2]
    assert path.read_bytes() == original and list(service.root.glob('*.tmp')) == []


def test_nonretryable_picture_record_denial_fails_immediately(world, monkeypatch):
    service, _, _ = world
    record = {'id': 'p-0123456789', 'status': 'pending'}
    service._save(record)
    path = service._json(record['id'])
    original, attempts, waits = path.read_bytes(), [], []

    def denied(source, target):
        attempts.append((source, target))
        raise PermissionError('synthetic nonretryable denial')

    monkeypatch.setattr(photos, '_retryable_replace_error', lambda error: False)
    monkeypatch.setattr(photos.os, 'replace', denied)
    monkeypatch.setattr(photos.time, 'sleep', waits.append)
    with pytest.raises(PermissionError):
        service._save({**record, 'status': 'ready'})
    assert len(attempts) == 1 and waits == []
    assert path.read_bytes() == original and list(service.root.glob('*.tmp')) == []


def test_shared_scene_does_not_unload_models_even_if_legacy_photo_option_is_enabled(world, monkeypatch):
    from core import llm
    service, _, _ = world
    service.unload_llm = True
    monkeypatch.setattr(llm, 'unload_models', lambda: pytest.fail('Shared pictures must not change loaded models.'))
    assert create(service)['status'] == 'ready'


@pytest.mark.parametrize('rejected', [False, None])
def test_uncertain_or_underage_reference_never_reaches_image_generator(world, rejected):
    service, checker, backend = world
    checker.results = deque([rejected])
    result = create(service)
    assert result['status'] == 'blocked' and not backend.calls and service.image_path(result['id']) is None


@pytest.mark.parametrize('rejected', [False, None])
def test_unchecked_generated_two_person_image_is_discarded(world, rejected):
    service, checker, _ = world
    checker.results = deque([True, rejected])
    result = create(service)
    assert result['status'] == 'blocked' and service.image_path(result['id']) is None


def test_consent_scene_and_reference_validation_precede_provider_calls(world):
    service, checker, backend = world
    for changes in ({'self_attested': False}, {'avatar_id': 'another'}, {'reference_png': b'not an image'}):
        with pytest.raises(PhotoError):
            create(service, **changes)
    with pytest.raises(PhotoBlocked):
        create(service, scene='a child in a playground')
    with pytest.raises(PhotoBlocked):
        create(service, scene='a celebrity in a cafe')
    assert not checker.calls and not backend.calls and not list(service.root.iterdir())


def test_shared_scene_never_overwrites_existing_image_id(world, monkeypatch):
    service, checker, backend = world
    existing = service.root / ('p-' + 'a' * 10 + '.png')
    existing.write_bytes(b'synthetic existing picture')
    monkeypatch.setattr(photos.uuid, 'uuid4', lambda: SimpleNamespace(hex='a' * 32))
    with pytest.raises(PhotoError, match='unique shared picture'):
        create(service)
    assert existing.read_bytes() == b'synthetic existing picture' and not checker.calls and not backend.calls


def test_shared_reference_is_owned_until_background_worker_finishes(tmp_path, monkeypatch):
    gate = state_gate.StateGate()
    monkeypatch.setattr(state_gate, 'STATE', gate)
    checker, backend = Checker(), Backend()
    service = PhotoService(tmp_path / 'photos', Avatars(), lambda level: backend, checker,
                           config={'imagegen': {'unload_llm_before_generate': False}})
    entered, release = threading.Event(), threading.Event()
    original = service._process_shared

    def pending(pid, data):
        entered.set()
        assert release.wait(5)
        original(pid, data)

    monkeypatch.setattr(service, '_process_shared', pending)
    try:
        result = create(service)
        assert entered.wait(3) and result['status'] in {'pending', 'running'}
        with pytest.raises(state_gate.StateBusy):
            with gate.exclusive():
                pytest.fail('Shared image work must block maintenance.')
    finally:
        release.set()
    assert gate.join_jobs(timeout=5) and not gate.active
    assert service.get(result['id'])['status'] == 'ready' and service._shared_pending == 0


def test_shared_queue_is_bounded_and_duplicate_request_never_starts_twice(tmp_path, monkeypatch):
    gate = state_gate.StateGate()
    monkeypatch.setattr(state_gate, 'STATE', gate)
    service = PhotoService(tmp_path / 'photos', Avatars(), lambda level: Backend(), Checker(),
                           config={'imagegen': {'unload_llm_before_generate': False}})
    entered, release = threading.Event(), threading.Event()
    monkeypatch.setattr(service, '_process_shared', lambda pid, data: (entered.set(), release.wait(5)))
    try:
        first = create(service)
        assert entered.wait(3)
        assert create(service)['id'] == first['id']
        create(service, request_id='shared_' + 'b' * 32)
        with pytest.raises(PhotoError, match='Wait for the current shared images'):
            create(service, request_id='shared_' + 'c' * 32)
        assert service._shared_pending == 2
    finally:
        release.set()
    assert gate.join_jobs(timeout=5) and service._shared_pending == 0


def test_unexpected_shared_worker_error_marks_request_failed_and_releases_gate(tmp_path, monkeypatch, caplog):
    gate = state_gate.StateGate()
    monkeypatch.setattr(state_gate, 'STATE', gate)
    service = PhotoService(tmp_path / 'photos', Avatars(), lambda level: Backend(), Checker(),
                           config={'imagegen': {'unload_llm_before_generate': False}})
    def fail(pid, data):
        raise RuntimeError('synthetic private reference content')
    monkeypatch.setattr(service, '_process_shared', fail)
    result = create(service)
    assert gate.join_jobs(timeout=5) and service._shared_pending == 0
    assert service.get(result['id'])['status'] == 'failed'
    assert 'synthetic private reference content' not in caplog.text
