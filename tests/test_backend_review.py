"""Backend review regressions, isolated from the running app and model services."""
import pytest

from core import companions, llm, notes
from core.state_gate import STATE, install_state_gate
from core.turns import PROCESS_ID, get_turn_service
from test_blueprints import create_app
from test_companion_api import active_avatar, engine, tmp_store, world  # noqa: F401
from test_companion_images import PNG
from test_companions import FakeMemoryManager, second_avatar


def test_background_helper_uses_saved_provider_before_the_first_chat(monkeypatch):
    monkeypatch.setattr(llm.LLMClient, '_instance', None)
    monkeypatch.setattr(llm, '_provider_registry', None)
    monkeypatch.setattr('core.config.get_config', lambda: {'llm': {
        'provider': 'custom', 'model': 'saved-model', 'providers': {
            'custom': {'base_url': 'http://saved.example/v1', 'api_key': 'fixture-key'}}}})
    client = llm.get_client()
    assert (client.base_url, client.model, client.api_key, client.provider_key) == (
        'http://saved.example/v1', 'saved-model', 'fixture-key', 'custom')


@pytest.fixture
def removable(world, monkeypatch):
    import core.memory
    avatar = active_avatar(world)
    sid = world.store.ensure_avatar_session(avatar['id'], 'Mara')
    world.store.save_message(sid, 'user', 'Preserve me until deletion succeeds')
    photo = world.photos.import_premade(avatar['id'], PNG, 'in a cafe', 'off', key='review:1')
    notes.NotesStore(world.avatars).write(avatar['id'], about_you='Private note')
    memory = FakeMemoryManager(['memory-1'])
    monkeypatch.setattr(core.memory, 'memory_manager', memory)
    app = create_app('companions', 'companion')
    install_state_gate(app)
    return avatar['id'], sid, photo['id'], memory, app.test_client()


@pytest.mark.parametrize('surface', ['companions', 'avatars'])
@pytest.mark.parametrize('busy_kind', ['worker', 'ledger', 'notes'])
def test_removal_refuses_busy_work_before_deleting_anything(world, removable, surface, busy_kind):
    avatar_id, sid, photo_id, memory, client = removable
    service = get_turn_service(world.store)
    lease = STATE.enter('altera-photo-sync') if busy_kind == 'worker' else None
    notes_service = notes.get_notes_service()
    if busy_kind == 'ledger':
        with service.connection() as conn:
            conn.execute("INSERT INTO chat_turns(request_id,session_id,fingerprint,status,process_id) VALUES(?,?,?,'streaming',?)",
                         ('busy-removal-review', sid, 'test-only', PROCESS_ID))
    if busy_kind == 'notes':
        notes_service._busy.add(avatar_id)
    try:
        response = client.delete(f'/api/{surface}/{avatar_id}')
        assert response.status_code == 409
        assert world.avatars.get(avatar_id) is not None
        assert world.store.get_session(sid) is not None
        assert world.photos.get(photo_id) is not None
        assert notes.NotesStore(world.avatars).read(avatar_id)['about_you'] == 'Private note'
        assert memory.deleted == []
        assert STATE.maintenance is False
    finally:
        if lease:
            lease.release()
        notes_service._busy.discard(avatar_id)


def test_legacy_avatar_removal_also_removes_its_owned_chats_and_memories(world, removable):
    avatar_id, sid, photo_id, memory, client = removable
    response = client.delete(f'/api/avatars/{avatar_id}')
    assert response.status_code == 200
    assert world.avatars.get(avatar_id) is None
    assert world.store.get_session(sid) is None
    assert world.photos.get(photo_id) is None
    assert memory.deleted == ['memory-1']


def test_unavailable_memory_store_does_not_partially_remove_companion(world, removable, monkeypatch):
    avatar_id, sid, photo_id, memory, client = removable
    def unavailable(**kwargs):
        raise RuntimeError('Memory storage unavailable')
    monkeypatch.setattr(memory.store, 'get_all', unavailable)
    response = client.delete(f'/api/companions/{avatar_id}')
    assert response.status_code == 500
    assert world.avatars.get(avatar_id) and world.store.get_session(sid) and world.photos.get(photo_id)
    assert memory.deleted == [] and STATE.maintenance is False


def test_missing_owned_avatar_never_uses_another_companion_or_saves_new_messages(world, engine):
    first = active_avatar(world)
    other = second_avatar(world)
    sid = world.store.ensure_avatar_session(first['id'])
    world.avatars.delete(first['id'])
    world.avatars.activate(other['id'])
    with pytest.raises(companions.CompanionError):
        companions.avatar_for_session(sid)
    with pytest.raises(companions.CompanionError):
        engine.send_message('Hello', sid)
    assert engine.llm.calls == [] and world.store.get_messages(sid) == []


def test_camera_uses_the_conversation_owner_when_another_companion_is_active(world):
    first, other = active_avatar(world), second_avatar(world)
    sid = world.store.ensure_avatar_session(first['id'])
    world.avatars.activate(other['id'])
    client = create_app('companion').test_client()
    response = client.post('/api/photos', json={'scene': 'in a cafe', 'session_id': sid})
    assert response.status_code == 202
    assert response.json['avatar_id'] == first['id']
    assert response.json['session_id'] == sid
    assert world.store.get_messages(sid)[-1].content == f"[[photo:{response.json['id']}]]"


@pytest.mark.parametrize('session_id,status', [(True, 400), ('1', 400), (999, 404), (2 ** 70, 400)])
def test_camera_rejects_invalid_sessions_before_starting_a_picture(world, session_id, status):
    active_avatar(world)
    response = create_app('companion').test_client().post('/api/photos', json={'scene': 'in a cafe', 'session_id': session_id})
    assert response.status_code == status
    assert world.photos.list() == [] and world.backend.calls == []
